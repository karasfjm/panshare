// Package p115 封装 115 网盘的 Web 接口。
//
// 设计取向：
//   - 只依赖标准库，不引第三方 SDK，接口路径集中在 endpoints.go；
//   - 全局限速（115 前面有阿里云 WAF，QPS 一高就按账号返回 405 拦截页）；
//   - 返回的字段做多键回退解析，个别字段改名不会整条链路崩掉。
package p115

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Error 是 115 的业务错误。
type Error struct {
	Op     string
	Status int // HTTP 状态码，405 通常是 WAF 限流
	Code   int // 115 的 errno
	Msg    string
	Body   string
}

func (e *Error) Error() string {
	if e.Msg != "" {
		return fmt.Sprintf("%s: %s", e.Op, e.Msg)
	}
	return fmt.Sprintf("%s: HTTP %d", e.Op, e.Status)
}

// IsAuth 判断是否登录态失效（cookies 过期 / 被踢下线）。
func IsAuth(err error) bool {
	e, ok := err.(*Error)
	if !ok {
		return false
	}
	if e.Code == 401 || e.Code == 990001 || e.Code == 911 {
		return true
	}
	msg := strings.ToLower(e.Msg)
	return strings.Contains(msg, "登录") && (strings.Contains(msg, "失效") ||
		strings.Contains(msg, "过期")) || strings.Contains(msg, "not login")
}

// IsWAF 判断是不是被 WAF 限流（HTTP 405）。
func IsWAF(err error) bool {
	e, ok := err.(*Error)
	return ok && e.Status == 405
}

type cacheItem struct {
	url    string
	header map[string]string
	expire time.Time
}

// Client 是一个账号的接口客户端。零值不可用，请用 New。
type Client struct {
	Cookies string
	UA      string

	http *http.Client

	mu       sync.Mutex
	nextAt   time.Time
	interval time.Duration

	cacheMu sync.Mutex
	cache   map[string]cacheItem

	// 目录 cid → 网盘绝对路径 / 目录树节点，事件监控拼路径用
	pathMu    sync.Mutex
	pathCache map[string]string
	dirCache  map[string]dirEntry

	uidMu sync.Mutex
	uid   string

	// 秒传签名用的 userkey（会变，取到就缓存；失败不缓存）
	uploadKeyMu sync.Mutex
	uploadKey   string
}

func New(cookies string) *Client {
	c := &Client{
		Cookies:  strings.TrimSpace(cookies),
		UA:       UserAgent,
		interval: 500 * time.Millisecond,
		cache:    map[string]cacheItem{},
	}
	c.http = &http.Client{Timeout: 40 * time.Second}
	return c
}

// SetQPS 设置全局限速（每秒请求数），<=0 表示不限速。
func (c *Client) SetQPS(qps int) {
	if qps <= 0 {
		c.interval = 0
		return
	}
	c.interval = time.Second / time.Duration(qps)
}

// SetCookies 换一份登录态（登录成功、cookie 更新后调用）。
func (c *Client) SetCookies(cookies string) {
	c.Cookies = strings.TrimSpace(cookies)
	c.cacheMu.Lock()
	c.cache = map[string]cacheItem{}
	c.cacheMu.Unlock()
	c.uidMu.Lock()
	c.uid = ""
	c.uidMu.Unlock()
	c.ForgetPaths()
}

func (c *Client) LoggedIn() bool { return c.Cookies != "" }

// wait 是全局限速：所有请求串行地排一个最小间隔。
func (c *Client) wait() {
	if c.interval <= 0 {
		return
	}
	c.mu.Lock()
	now := time.Now()
	if c.nextAt.After(now) {
		d := c.nextAt.Sub(now)
		c.mu.Unlock()
		time.Sleep(d)
		c.mu.Lock()
	}
	c.nextAt = time.Now().Add(c.interval)
	c.mu.Unlock()
}

type requestOptions struct {
	method  string
	form    url.Values
	referer string
	extra   map[string]string
	// ua 覆盖默认 UA。分享直链等 app 通道只认 115 客户端 UA，浏览器 UA 会被拒。
	ua string
	// noReferer 不发送 Referer（app 通道按官方客户端原样发，带了反而可能被挑）
	noReferer bool
	// 下载直链等场景需要原始 body
	raw bool
	// 部分通道（安卓取直链）官方客户端不带 Cookie，带上反而可能被 WAF 认成异常
	noCookie bool
}

func (c *Client) do(ctx context.Context, rawURL string, opt requestOptions) ([]byte, int, error) {
	data, _, status, err := c.doReq(ctx, rawURL, opt)
	return data, status, err
}

// doReq 与 do 相同，但把响应头也带回来：115 的直链（f=3）要求播放时
// 带上「取链接那次响应」里 Set-Cookie 给的 cookie。
func (c *Client) doReq(ctx context.Context, rawURL string, opt requestOptions) ([]byte, http.Header, int, error) {
	if !c.LoggedIn() {
		return nil, nil, 0, &Error{Op: "115", Msg: "账号未登录，请先在「115 助手」添加账号"}
	}
	method := opt.method
	if method == "" {
		method = http.MethodGet
	}
	var body io.Reader
	if opt.form != nil {
		body = strings.NewReader(opt.form.Encode())
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, body)
	if err != nil {
		return nil, nil, 0, err
	}
	ua := c.UA
	if opt.ua != "" {
		ua = opt.ua
	}
	req.Header.Set("User-Agent", ua)
	if !opt.noCookie {
		req.Header.Set("Cookie", c.Cookies)
	}
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9")
	ref := opt.referer
	if ref == "" && !opt.noReferer {
		ref = "https://115.com/"
	}
	if ref != "" {
		req.Header.Set("Referer", ref)
	}
	if opt.form != nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	for k, v := range opt.extra {
		req.Header.Set(k, v)
	}

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		c.wait()
		resp, err := c.http.Do(req)
		if err != nil {
			lastErr = err
			time.Sleep(time.Duration(attempt+1) * 500 * time.Millisecond)
			continue
		}
		data, readErr := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
		hdr := resp.Header.Clone()
		resp.Body.Close()
		if readErr != nil {
			lastErr = readErr
			continue
		}
		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == 405 {
			// WAF：退避后重试一次，别把账号打成更长的封禁
			lastErr = &Error{Op: "115", Status: resp.StatusCode, Msg: "请求过于频繁（WAF 限流），请调低 API QPS", Body: truncate(string(data), 300)}
			time.Sleep(time.Duration(2+attempt*3) * time.Second)
			continue
		}
		if resp.StatusCode >= 400 {
			return data, hdr, resp.StatusCode, &Error{
				Op: "115", Status: resp.StatusCode, Msg: http.StatusText(resp.StatusCode),
				Body: truncate(string(data), 300),
			}
		}
		return data, hdr, resp.StatusCode, nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("请求失败")
	}
	return nil, nil, 0, lastErr
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func (c *Client) getJSON(ctx context.Context, rawURL string, extra map[string]string) (map[string]any, error) {
	data, _, err := c.do(ctx, rawURL, requestOptions{extra: extra})
	if err != nil {
		return nil, err
	}
	return parseJSON(data)
}

func (c *Client) postForm(ctx context.Context, rawURL string, form url.Values) (map[string]any, error) {
	data, _, err := c.do(ctx, rawURL, requestOptions{method: http.MethodPost, form: form})
	if err != nil {
		return nil, err
	}
	return parseJSON(data)
}

// postFormRaw 同 postForm，但保留原始 body 与响应头（下载接口要自己解密）。
// extra 用来覆盖请求头 —— 下载接口必须用「播放器的 UA」去取链接，
// 否则 115 下发的 cookie 会和播放时的 UA 对不上（CDN 回 ua not match cookie）。
// postJSON 用 JSON body 发 POST（少数接口只认 json）。
func (c *Client) postJSON(ctx context.Context, rawURL, body string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, rawURL, strings.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", c.UA)
	if c.Cookies != "" {
		req.Header.Set("Cookie", c.Cookies)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	return parseJSON(data)
}

func (c *Client) postFormRaw(ctx context.Context, rawURL string, form url.Values, extra map[string]string) ([]byte, http.Header, error) {
	data, hdr, _, err := c.doReq(ctx, rawURL, requestOptions{method: http.MethodPost, form: form, extra: extra})
	return data, hdr, err
}

// postFormNoCookie 与 postFormRaw 相同，但不带账号 Cookie。
// 安卓取直链通道官方就是不带的，带上反而可能被 WAF 判成异常请求。
func (c *Client) postFormNoCookie(ctx context.Context, rawURL string, form url.Values, extra map[string]string) ([]byte, http.Header, error) {
	data, hdr, _, err := c.doReq(ctx, rawURL, requestOptions{method: http.MethodPost, form: form, extra: extra, noCookie: true})
	return data, hdr, err
}

func parseJSON(data []byte) (map[string]any, error) {
	text := strings.TrimSpace(string(data))
	if text == "" {
		return map[string]any{}, nil
	}
	if strings.HasPrefix(text, "<") {
		return nil, &Error{Op: "115", Msg: "接口返回了 HTML（多为 WAF 拦截或登录失效）", Body: truncate(text, 300)}
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(text), &m); err != nil {
		var arr []any
		if err2 := json.Unmarshal([]byte(text), &arr); err2 == nil {
			return map[string]any{"data": arr}, nil
		}
		return nil, &Error{Op: "115", Msg: "响应解析失败", Body: truncate(text, 300)}
	}
	return m, nil
}

var errSep = fmt.Errorf("没有数据")

// check 校验 state 字段，把 115 的业务错误转成 *Error。
func check(op string, m map[string]any) error {
	st, ok := m["state"]
	if !ok {
		return nil
	}
	okv := false
	switch v := st.(type) {
	case bool:
		okv = v
	case float64:
		okv = v != 0
	case string:
		okv = v == "1" || strings.EqualFold(v, "true")
	}
	if okv {
		return nil
	}
	msg := str(m, "error", "error_msg", "message", "msg", "errmsg")
	return &Error{Op: op, Code: int(num(m, "errno", "error_code", "code")), Msg: msg}
}

// ---------------------------------------------------------------- map 取值辅助

func str(m map[string]any, keys ...string) string {
	for _, k := range keys {
		v, ok := m[k]
		if !ok || v == nil {
			continue
		}
		switch t := v.(type) {
		case string:
			if t != "" {
				return t
			}
		case float64:
			if t != 0 {
				return strconv.FormatInt(int64(t), 10)
			}
		case json.Number:
			return t.String()
		case bool:
			if t {
				return "1"
			}
		}
	}
	return ""
}

func num(m map[string]any, keys ...string) int64 {
	for _, k := range keys {
		v, ok := m[k]
		if !ok || v == nil {
			continue
		}
		switch t := v.(type) {
		case float64:
			return int64(t)
		case string:
			if n, err := strconv.ParseInt(strings.TrimSpace(t), 10, 64); err == nil {
				return n
			}
		case json.Number:
			if n, err := t.Int64(); err == nil {
				return n
			}
		}
	}
	return 0
}

func boolv(m map[string]any, keys ...string) (bool, bool) {
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			switch t := v.(type) {
			case bool:
				return t, true
			case float64:
				return t != 0, true
			case string:
				return t == "1" || strings.EqualFold(t, "true") || t == "yes", true
			}
		}
	}
	return false, false
}

func subMap(m map[string]any, key string) map[string]any {
	if v, ok := m[key].(map[string]any); ok {
		return v
	}
	return map[string]any{}
}

func listOf(m map[string]any, keys ...string) []map[string]any {
	for _, key := range keys {
		if out := listOfKey(m, key); len(out) > 0 {
			return out
		}
	}
	return nil
}

func listOfKey(m map[string]any, key string) []map[string]any {
	raw, ok := m[key]
	if !ok {
		return nil
	}
	arr, ok := raw.([]any)
	if !ok {
		return nil
	}
	out := make([]map[string]any, 0, len(arr))
	for _, item := range arr {
		if d, ok := item.(map[string]any); ok {
			out = append(out, d)
		}
	}
	return out
}

// ---------------------------------------------------------------- 直链缓存

func (c *Client) cacheGet(key string) (string, map[string]string, bool) {
	c.cacheMu.Lock()
	defer c.cacheMu.Unlock()
	it, ok := c.cache[key]
	if !ok || time.Now().After(it.expire) {
		return "", nil, false
	}
	return it.url, it.header, true
}

func (c *Client) cachePut(key, u string, h map[string]string, ttl time.Duration) {
	c.cacheMu.Lock()
	defer c.cacheMu.Unlock()
	if len(c.cache) > 500 {
		c.cache = map[string]cacheItem{}
	}
	c.cache[key] = cacheItem{url: u, header: h, expire: time.Now().Add(ttl)}
}

func (c *Client) cacheDrop() {
	c.cacheMu.Lock()
	c.cache = map[string]cacheItem{}
	c.cacheMu.Unlock()
}

// postFormMulti 支持 115 的 fid[0]、hash[0] 这类数组参数。
func postFormMulti(ctx context.Context, c *Client, rawURL string, base url.Values,
	prefix string, ids []string) (map[string]any, error) {
	form := url.Values{}
	for k, v := range base {
		form[k] = v
	}
	for i, id := range ids {
		form.Set(fmt.Sprintf("%s[%d]", prefix, i), id)
	}
	return c.postForm(ctx, rawURL, form)
}

var _ = bytes.MinRead
