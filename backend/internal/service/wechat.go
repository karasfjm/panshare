package service

import (
	"bytes"
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

// 微信通知：两种通道，填了哪个发哪个。
//
//  1. 企业微信自建应用（CorpID + Secret + AgentID）
//     GET  https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=…&corpsecret=…
//     POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=…
//  2. 企业微信群机器人（群设置里加的 Webhook）
//     POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…
//
// 逻辑参考 MoviePilot 的 WeChat 模块：access_token 缓存到过期前、
// errcode 42001/40014/40001 视为 token 失效后强制刷新重试一次、
// 文本按 2048 字节上限分片（企业微信 text 消息的硬限制）。
//
// qyapi.weixin.qq.com 国内直连可达，所以默认不走 Telegram 那个海外代理；
// 但容器里若设了 HTTPS_PROXY，仍会跟随（ProxyFromEnvironment）。
//
// 注意：自建应用要求调用方出口 IP 在「企业可信 IP」白名单里，否则报 60020。
// 出口 IP 会变，可用「动态企微可信IP」插件自动维护这份白名单。
type WeChat struct {
	app *App

	mu        sync.Mutex
	client    *http.Client
	token     string
	tokenExp  time.Time
	lastErr   string
	lastErrAt time.Time
	lastOK    time.Time
}

type wxTokenResp struct {
	ErrCode     int    `json:"errcode"`
	ErrMsg      string `json:"errmsg"`
	AccessToken string `json:"access_token"`
	ExpiresIn   int    `json:"expires_in"`
}

type wxSendResp struct {
	ErrCode int    `json:"errcode"`
	ErrMsg  string `json:"errmsg"`
}

const wxAPI = "https://qyapi.weixin.qq.com"

// appConfigured 判断企业微信自建应用的三个必填项是否齐全。
func (w *WeChat) appConfigured() bool {
	s := w.app.Settings()
	return strings.TrimSpace(s.WeChatCorpID) != "" &&
		strings.TrimSpace(s.WeChatSecret) != "" &&
		strings.TrimSpace(s.WeChatAgentID) != ""
}

func (w *WeChat) robotConfigured() bool {
	return strings.TrimSpace(w.app.Settings().WeChatRobotURL) != ""
}

func (w *WeChat) enabled() bool {
	return w.app.Settings().WeChatEnabled && (w.appConfigured() || w.robotConfigured())
}

func (w *WeChat) httpClient() *http.Client {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.client == nil {
		// 空代理串 = 跟随环境变量；国内直连 qyapi 即可
		w.client, _ = newProxyClient("", 15*time.Second)
	}
	return w.client
}

// scrub 把错误文本里的 Secret / token / 机器人 key 抹掉。
// HTTP 客户端报错会把整条 URL 带出来，不处理就等于把密钥写进日志和页面。
func (w *WeChat) scrub(err error) error {
	if err == nil {
		return nil
	}
	s := w.app.Settings()
	msg := err.Error()
	if v := strings.TrimSpace(s.WeChatSecret); v != "" {
		msg = strings.ReplaceAll(msg, v, "<secret>")
	}
	if v := strings.TrimSpace(s.WeChatRobotURL); v != "" {
		if i := strings.Index(v, "key="); i >= 0 {
			msg = strings.ReplaceAll(msg, v[i+4:], "<key>")
		}
	}
	w.mu.Lock()
	tok := w.token
	w.mu.Unlock()
	if tok != "" {
		msg = strings.ReplaceAll(msg, tok, "<token>")
	}
	return fmt.Errorf("%s", msg)
}

func (w *WeChat) setErr(err error) {
	if err == nil {
		return
	}
	msg := w.scrub(err).Error()
	w.mu.Lock()
	w.lastErr = msg
	w.lastErrAt = time.Now()
	w.mu.Unlock()
}

func (w *WeChat) setOK() {
	w.mu.Lock()
	w.lastOK = time.Now()
	w.lastErr = ""
	w.lastErrAt = time.Time{}
	w.mu.Unlock()
}

// wantsKinds 判断这条事件类型要不要推微信。
// 留空 = 全部；「-」= 一个都不推；否则按逗号列表匹配。
func (w *WeChat) wantsKinds(kind string) bool {
	list := strings.TrimSpace(w.app.Settings().WeChatNotifyKinds)
	if list == "" {
		return true
	}
	if list == "-" {
		return false
	}
	for _, k := range strings.Split(list, ",") {
		if strings.TrimSpace(k) == kind {
			return true
		}
	}
	return false
}

// Notify 推送一条消息。没配置就静默跳过。
// 失败只记在 lastErr（页面能看到），不写事件——事件本身会触发推送，会打转。
func (w *WeChat) Notify(level, kind, message string) {
	if !w.enabled() || !w.wantsKinds(kind) {
		return
	}
	icon := map[string]string{
		"success": "✅", "info": "ℹ️", "warn": "⚠️", "error": "❌",
	}[level]
	if icon == "" {
		icon = "•"
	}
	text := icon + " " + message

	var errs []string
	if w.appConfigured() {
		if err := w.sendAppText(text); err != nil {
			errs = append(errs, "企业微信应用："+err.Error())
		}
	}
	if w.robotConfigured() {
		if err := w.sendRobotText(text); err != nil {
			errs = append(errs, "群机器人："+err.Error())
		}
	}
	if len(errs) > 0 {
		w.setErr(fmt.Errorf("%s", strings.Join(errs, "；")))
		return
	}
	w.setOK()
}

// Test 同步发一条测试消息，把真实错误原样返回给页面。
func (w *WeChat) Test() error {
	if !w.appConfigured() && !w.robotConfigured() {
		return fmt.Errorf("还没填企业微信应用（CorpID / Secret / AgentID）或群机器人 Webhook")
	}
	const text = "✅ PanShare 测试消息：微信通知链路正常"

	var errs []string
	if w.appConfigured() {
		if err := w.sendAppText(text); err != nil {
			errs = append(errs, "企业微信应用："+err.Error())
		}
	}
	if w.robotConfigured() {
		if err := w.sendRobotText(text); err != nil {
			errs = append(errs, "群机器人："+err.Error())
		}
	}
	if len(errs) > 0 {
		err := fmt.Errorf("%s", strings.Join(errs, "；"))
		w.setErr(err)
		return err
	}
	w.setOK()
	return nil
}

// Status 给页面看的运行态。
func (w *WeChat) Status() map[string]any {
	s := w.app.Settings()
	w.mu.Lock()
	lastErr, errAt, okAt := w.lastErr, w.lastErrAt, w.lastOK
	w.mu.Unlock()

	out := map[string]any{
		"enabled":          w.enabled(),
		"app_configured":   w.appConfigured(),
		"robot_configured": w.robotConfigured(),
		"to_user":          firstNonEmptyStr(strings.TrimSpace(s.WeChatToUser), "@all"),
		"last_error":       lastErr,
	}
	if !errAt.IsZero() {
		out["last_error_at"] = errAt.Format("2006-01-02 15:04:05")
	}
	if !okAt.IsZero() {
		out["last_ok_at"] = okAt.Format("2006-01-02 15:04:05")
	}
	return out
}

// ---------------------------------------------------------------- 企业微信应用

// accessToken 取缓存里的 token，没有或过期就重新拉。
func (w *WeChat) accessToken(force bool) (string, error) {
	s := w.app.Settings()
	corp := strings.TrimSpace(s.WeChatCorpID)
	secret := strings.TrimSpace(s.WeChatSecret)
	if corp == "" || secret == "" {
		return "", fmt.Errorf("还没填 CorpID / Secret")
	}

	w.mu.Lock()
	if !force && w.token != "" && time.Now().Before(w.tokenExp) {
		tok := w.token
		w.mu.Unlock()
		return tok, nil
	}
	w.mu.Unlock()

	u := wxAPI + "/cgi-bin/gettoken?corpid=" + url.QueryEscape(corp) +
		"&corpsecret=" + url.QueryEscape(secret)
	resp, err := w.httpClient().Get(u)
	if err != nil {
		return "", fmt.Errorf("连不上 qyapi.weixin.qq.com：%v", w.scrub(err))
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))

	var r wxTokenResp
	if err := json.Unmarshal(body, &r); err != nil {
		return "", fmt.Errorf("企业微信返回看不懂：%s", snippet(body))
	}
	if r.ErrCode != 0 {
		return "", fmt.Errorf("取 access_token 失败：%d %s%s", r.ErrCode, r.ErrMsg, wxHint(r.ErrCode))
	}
	if r.AccessToken == "" {
		return "", fmt.Errorf("企业微信没有返回 access_token")
	}

	// 提前 5 分钟过期，避开边界上刚好失效
	ttl := time.Duration(r.ExpiresIn) * time.Second
	if ttl > 5*time.Minute {
		ttl -= 5 * time.Minute
	}
	w.mu.Lock()
	w.token = r.AccessToken
	w.tokenExp = time.Now().Add(ttl)
	w.mu.Unlock()
	return r.AccessToken, nil
}

func (w *WeChat) invalidateToken() {
	w.mu.Lock()
	w.token = ""
	w.tokenExp = time.Time{}
	w.mu.Unlock()
}

// sendAppText 按 2048 字节分片后逐条发。
func (w *WeChat) sendAppText(text string) error {
	for _, chunk := range splitByBytes(text, 2000) {
		if err := w.sendApp(chunk, false); err != nil {
			return err
		}
	}
	return nil
}

func (w *WeChat) sendApp(text string, retried bool) error {
	s := w.app.Settings()
	aid, err := strconv.Atoi(strings.TrimSpace(s.WeChatAgentID))
	if err != nil {
		return fmt.Errorf("AgentID 要填数字（企业微信应用详情页的 AgentId）")
	}
	tok, err := w.accessToken(false)
	if err != nil {
		return err
	}
	to := firstNonEmptyStr(strings.TrimSpace(s.WeChatToUser), "@all")

	payload := map[string]any{
		"touser":                 to,
		"msgtype":                "text",
		"agentid":                aid,
		"text":                   map[string]string{"content": text},
		"safe":                   0,
		"enable_id_trans":        0,
		"enable_duplicate_check": 0,
	}
	b, _ := json.Marshal(payload)
	u := wxAPI + "/cgi-bin/message/send?access_token=" + url.QueryEscape(tok)
	resp, err := w.httpClient().Post(u, "application/json", bytes.NewReader(b))
	if err != nil {
		return fmt.Errorf("发送失败：%v", w.scrub(err))
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))

	var r wxSendResp
	if err := json.Unmarshal(body, &r); err != nil {
		return fmt.Errorf("企业微信返回看不懂：%s", snippet(body))
	}
	// token 失效类错误：清缓存重试一次
	if !retried && (r.ErrCode == 42001 || r.ErrCode == 40014 || r.ErrCode == 40001) {
		w.invalidateToken()
		return w.sendApp(text, true)
	}
	if r.ErrCode != 0 {
		return fmt.Errorf("发送失败：%d %s%s", r.ErrCode, r.ErrMsg, wxHint(r.ErrCode))
	}
	return nil
}

// ---------------------------------------------------------------- 群机器人

// sendRobotText 同样按 2048 字节分片。
func (w *WeChat) sendRobotText(text string) error {
	u, err := w.robotEndpoint()
	if err != nil {
		return err
	}
	for _, chunk := range splitByBytes(text, 2000) {
		payload := map[string]any{
			"msgtype": "text",
			"text":    map[string]string{"content": chunk},
		}
		b, _ := json.Marshal(payload)
		resp, err := w.httpClient().Post(u, "application/json", bytes.NewReader(b))
		if err != nil {
			return fmt.Errorf("发送失败：%v", w.scrub(err))
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		resp.Body.Close()

		var r wxSendResp
		if err := json.Unmarshal(body, &r); err != nil {
			return fmt.Errorf("群机器人返回看不懂：%s", snippet(body))
		}
		if r.ErrCode != 0 {
			return fmt.Errorf("发送失败：%d %s%s", r.ErrCode, r.ErrMsg, wxHint(r.ErrCode))
		}
	}
	return nil
}

// robotEndpoint 支持两种填法：完整的 webhook 地址，或只填 key。
func (w *WeChat) robotEndpoint() (string, error) {
	raw := strings.TrimSpace(w.app.Settings().WeChatRobotURL)
	if raw == "" {
		return "", fmt.Errorf("还没填群机器人 Webhook")
	}
	if !strings.Contains(raw, "://") {
		return wxAPI + "/cgi-bin/webhook/send?key=" + url.QueryEscape(raw), nil
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "", fmt.Errorf("Webhook 地址填错了，应形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("Webhook 地址必须以 http:// 或 https:// 开头")
	}
	return raw, nil
}

// ---------------------------------------------------------------- 小工具

// wxHint 给常见错误码补一句人话，省得对着 errcode 猜。
func wxHint(code int) string {
	switch code {
	case 60020:
		return "（出口 IP 不在「企业可信 IP」白名单里，可用「动态企微可信IP」插件自动维护）"
	case 40001, 40014, 42001:
		return "（access_token 无效，多与 Secret 填错或已重置有关）"
	case 40013:
		return "（CorpID 不对）"
	case 60011:
		return "（没有权限操作该应用，检查 AgentID 与 Secret 是否配套）"
	case 81013:
		return "（接收成员不存在，检查 touser 或改用 @all）"
	case 45009:
		return "（接口调用超过频率限制）"
	}
	return ""
}

// splitByBytes 按字节上限切分文本。
//
// 企业微信 text 消息上限 2048 字节（UTF-8），超了整条会被拒。
// 优先按行切；单行本身超限时按 rune 硬切，避免把汉字切成半个。
func splitByBytes(text string, limit int) []string {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}
	if len([]byte(text)) <= limit {
		return []string{text}
	}

	var out []string
	var cur strings.Builder
	flush := func() {
		s := strings.TrimRight(cur.String(), "\n")
		if strings.TrimSpace(s) != "" {
			out = append(out, s)
		}
		cur.Reset()
	}

	for _, line := range strings.Split(text, "\n") {
		if len([]byte(line)) > limit {
			flush()
			var part []rune
			plen := 0
			for _, r := range line {
				rl := len(string(r))
				if plen+rl > limit && plen > 0 {
					out = append(out, string(part))
					part = part[:0]
					plen = 0
				}
				part = append(part, r)
				plen += rl
			}
			if plen > 0 {
				cur.WriteString(string(part))
				cur.WriteByte('\n')
			}
			continue
		}
		if cur.Len()+len([]byte(line))+1 > limit {
			flush()
		}
		cur.WriteString(line)
		cur.WriteByte('\n')
	}
	flush()
	return out
}

func snippet(b []byte) string {
	s := strings.TrimSpace(string(b))
	if len(s) > 300 {
		s = s[:300] + "…"
	}
	if s == "" {
		return "（空响应）"
	}
	return s
}
