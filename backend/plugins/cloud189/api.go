// Package main 是天翼云盘插件：自带独立的 HTTP 服务与页面，
// 由 PanShare 核心按需安装、启动，并通过 /api/plugins/cloud189/proxy 反代。
package main

import (
	"crypto/hmac"
	"crypto/md5"
	"crypto/sha1"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// 天翼云盘 TV 端凭据（对齐 cloudpan189-share 的实现）
const (
	apiURL       = "https://api.cloud.189.cn"
	appKey       = "600100885"
	appSecret    = "fe5734c74c2f96a38157f420b32dc995"
	tvVersion    = "6.5.5"
	tvChannel    = "home02"
	userAgent    = "EcloudTV/6.5.5 (PJX110; unknown; home02) Android/35"
	acceptHeader = "application/json;charset=UTF-8"

	rootFolderID = "-11"
	familyNode   = "fam"
)

var httpClient = &http.Client{
	Timeout: 30 * time.Second,
	Transport: &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	},
}

func md5Hex(s string) string {
	sum := md5.Sum([]byte(s))
	return hex.EncodeToString(sum[:])
}

func nowMS() int64 { return time.Now().UnixNano() / int64(time.Millisecond) }

func clientSuffix() url.Values {
	v := url.Values{}
	v.Set("clientType", "FAMILY_TV")
	v.Set("version", tvVersion)
	v.Set("channelId", tvChannel)
	v.Set("clientSn", "unknown")
	v.Set("model", "PJX110")
	v.Set("osFamily", "Android")
	v.Set("osVersion", "35")
	v.Set("networkAccessMode", "WIFI")
	v.Set("telecomsOperator", "46011")
	return v
}

// signQuery 业务接口签名：参数 + AccessToken/Timestamp/AppKey 排序拼串后 MD5。
func signQuery(params url.Values, accessToken string) (string, http.Header) {
	values := url.Values{}
	for k, vs := range params {
		for _, v := range vs {
			values.Add(k, v)
		}
	}
	if accessToken != "" {
		values.Set("AccessToken", accessToken)
	}
	ts := fmt.Sprint(nowMS())
	values.Set("Timestamp", ts)
	values.Set("AppKey", appKey)

	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		vs := append([]string(nil), values[k]...)
		sort.Strings(vs)
		for _, v := range vs {
			parts = append(parts, k+"="+v)
		}
	}
	raw := strings.Join(parts, "&")

	h := http.Header{}
	h.Set("Signature", md5Hex(raw))
	h.Set("Sign-Type", "1")
	h.Set("Timestamp", ts)
	h.Set("AppKey", appKey)
	if accessToken != "" {
		h.Set("AccessToken", accessToken)
	}
	q := url.Values{}
	for _, k := range keys {
		for _, v := range values[k] {
			q.Add(k, v)
		}
	}
	return q.Encode(), h
}

// signNamed 换 token 时 token 以自定义键名参与签名（例如 sessionKey）。
func signNamed(params url.Values, key, token string) (string, http.Header) {
	values := url.Values{}
	for k, vs := range params {
		for _, v := range vs {
			values.Add(k, v)
		}
	}
	ts := fmt.Sprint(nowMS())
	values.Set(key, token)
	values.Set("Timestamp", ts)
	values.Set("AppKey", appKey)

	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		vs := append([]string(nil), values[k]...)
		sort.Strings(vs)
		for _, v := range vs {
			parts = append(parts, k+"="+v)
		}
	}
	h := http.Header{}
	h.Set("Signature", md5Hex(strings.Join(parts, "&")))
	h.Set("Sign-Type", "1")
	h.Set("Timestamp", ts)
	h.Set("AppKey", appKey)
	h.Set(key, token)

	q := url.Values{}
	for _, k := range keys {
		for _, v := range values[k] {
			q.Add(k, v)
		}
	}
	return q.Encode(), h
}

// appKeySignature 登录类接口用的 HMAC-SHA1 签名。
func appKeySignature(fullURL, method string) http.Header {
	ts := fmt.Sprint(nowMS())
	path := fullURL
	if i := strings.Index(fullURL, "://"); i >= 0 {
		rest := fullURL[i+3:]
		if j := strings.Index(rest, "/"); j >= 0 {
			path = rest[j:]
		} else {
			path = "/"
		}
	}
	if i := strings.IndexAny(path, "?#"); i >= 0 {
		path = path[:i]
	}
	data := fmt.Sprintf("AppKey=%s&Operate=%s&RequestURI=%s&Timestamp=%s",
		appKey, strings.ToUpper(method), path, ts)
	mac := hmac.New(sha1.New, []byte(appSecret))
	mac.Write([]byte(data))
	h := http.Header{}
	h.Set("Timestamp", ts)
	h.Set("X-Request-ID", randomID())
	h.Set("AppKey", appKey)
	h.Set("AppSignature", strings.ToUpper(hex.EncodeToString(mac.Sum(nil))))
	return h
}

var codeRe = regexp.MustCompile(`<(code|message|msg|res_code|res_message)>(.*?)</(code|message|msg|res_code|res_message)>`)

func parseBody(data []byte) (map[string]any, error) {
	text := strings.TrimSpace(string(data))
	if text == "" {
		return map[string]any{}, nil
	}
	if strings.HasPrefix(text, "<") {
		out := map[string]any{}
		for _, m := range codeRe.FindAllStringSubmatch(text, -1) {
			out[m[1]] = m[2]
		}
		if len(out) == 0 {
			return nil, fmt.Errorf("云盘返回了无法解析的响应")
		}
		return out, nil
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(text), &m); err != nil {
		return nil, fmt.Errorf("响应解析失败: %s", truncate(text, 200))
	}
	return m, nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// okCode 判断一个「状态码」字段是否表示成功。
// 天翼不同接口分别用 "0"、0、"SUCCESS" 表示成功，早先只排除空串会把 errorCode="0"
// 当成错误抛出去，扫码成功后就被判成失败。
func okCode(s string) bool {
	switch strings.ToUpper(strings.TrimSpace(s)) {
	case "", "0", "SUCCESS", "OK", "TRUE":
		return true
	}
	return false
}

func respError(m map[string]any) string {
	if v, ok := m["res_code"]; ok && !okCode(fmt.Sprint(v)) {
		return fmt.Sprintf("res_code=%v %v", v, m["res_message"])
	}
	if v, ok := m["code"]; ok && !okCode(fmt.Sprint(v)) {
		return fmt.Sprintf("code=%v %v", v, firstOf(m, "msg", "message"))
	}
	if v, ok := m["errorCode"]; ok && !okCode(fmt.Sprint(v)) {
		return fmt.Sprintf("%v %v", v, firstOf(m, "errorMsg", "message"))
	}
	return ""
}

func firstOf(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			if s := strings.TrimSpace(fmt.Sprint(v)); s != "" {
				return s
			}
		}
	}
	return ""
}

func dig(m map[string]any, keys ...string) map[string]any {
	for _, k := range keys {
		if v, ok := m[k].(map[string]any); ok {
			return v
		}
	}
	return m
}

// firstInt 直接取数值字段。
//
// 不能写成 asInt(firstOf(...))：firstOf 会把 float64 用 %v 转成字符串，
// 大数（token 的 expiresIn、文件大小）会变成 "1.797691454e+12" 这种科学计数法，
// 再解析只剩 "1"，到期时间就会算成「1 秒后」。
func firstInt(m map[string]any, keys ...string) int64 {
	for _, k := range keys {
		v, ok := m[k]
		if !ok || v == nil {
			continue
		}
		switch t := v.(type) {
		case float64:
			return int64(t)
		case int64:
			return t
		case json.Number:
			n, _ := t.Int64()
			return n
		case string:
			var n int64
			fmt.Sscan(strings.TrimSpace(t), &n)
			return n
		}
	}
	return 0
}

// numStr 取数值字段并还原成整数文本。
//
// 不能直接用 firstOf：JSON 解出来的 float64 走 %v 会变成 "5.79739066e+08"
// 这种科学计数法，拼进 familyId 去请求家庭云，服务端直接回
// InternalError familyStatusCheck() —— 看着像「没有权限」，其实只是 id 被写坏了。
func numStr(m map[string]any, keys ...string) string {
	for _, k := range keys {
		v, ok := m[k]
		if !ok || v == nil {
			continue
		}
		switch t := v.(type) {
		case float64:
			return strconv.FormatFloat(t, 'f', 0, 64)
		case int64:
			return strconv.FormatInt(t, 10)
		case int:
			return strconv.Itoa(t)
		case json.Number:
			return t.String()
		case string:
			if s := strings.TrimSpace(t); s != "" {
				return s
			}
		}
	}
	return ""
}

// fixNum 把历史数据里存坏的科学计数法 id 还原成整数（老配置可能存过
// fam:5.79739066e+08），不是这种形状就原样返回。
func fixNum(s string) string {
	if !strings.ContainsAny(s, "eE") {
		return s
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || f <= 0 {
		return s
	}
	return strconv.FormatFloat(f, 'f', 0, 64)
}

func asInt(v any) int64 {
	switch t := v.(type) {
	case float64:
		return int64(t)
	case string:
		var n int64
		fmt.Sscan(t, &n)
		return n
	case json.Number:
		n, _ := t.Int64()
		return n
	}
	return 0
}

// CloudClient 是一个账号的接口客户端。
type CloudClient struct {
	AccessToken string
	ExpiresAt   int64 // 毫秒时间戳
	Refresh     func() (string, error)
	mu          sync.Mutex
}

func (c *CloudClient) get(api string, params url.Values) (map[string]any, error) {
	c.mu.Lock()
	token := c.AccessToken
	c.mu.Unlock()
	if token == "" {
		return nil, fmt.Errorf("账号还没有登录")
	}
	m, err := c.getOnce(api, params, token)
	if err != nil {
		return nil, err
	}
	msg := respError(m)
	if msg != "" && isTokenError(msg) && c.Refresh != nil {
		fresh, rerr := c.Refresh()
		if rerr != nil {
			return nil, fmt.Errorf("%s（自动续期失败：%v）", msg, rerr)
		}
		c.mu.Lock()
		c.AccessToken = fresh
		c.mu.Unlock()
		return c.getOnce(api, params, fresh)
	}
	if msg != "" {
		return nil, fmt.Errorf("%s", msg)
	}
	return m, nil
}

func (c *CloudClient) getOnce(api string, params url.Values, token string) (map[string]any, error) {
	q, headers := signQuery(params, token)
	req, err := http.NewRequest(http.MethodGet, apiURL+api+"?"+q, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", acceptHeader)
	for k, vs := range headers {
		for _, v := range vs {
			req.Header.Set(k, v)
		}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	return parseBody(body)
}

func isTokenError(msg string) bool {
	l := strings.ToLower(msg)
	return strings.Contains(l, "invalidaccesstoken") || strings.Contains(l, "expired") ||
		strings.Contains(l, "invalidtoken") || strings.Contains(msg, "登录") && strings.Contains(msg, "失效")
}

// hasTokenError 判断错误文本里是否带 token 失效特征（Play 时兜底）。
func hasTokenError(err error) bool {
	if err == nil {
		return false
	}
	return isTokenError(err.Error())
}

var _ = strings.TrimSpace
