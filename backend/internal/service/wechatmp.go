package service

// 微信服务号（公众号）通道：推送 + 公网回调。
//
// 推送用「模板消息」，不用客服消息——客服消息只有 48 小时互动窗口，
// 半夜跑完的任务根本发不出去。
//
//	GET  https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=…&secret=…
//	POST https://api.weixin.qq.com/cgi-bin/message/template/send?access_token=…
//
// 回调地址填在公众号后台「设置与开发 → 基本配置 → 服务器配置」，
// 必须是公网可达的 http(s)，所以走 compose 的公网地址：
//
//	GET  /api/wechat/mp/callback?signature=…&timestamp=…&nonce=…&echostr=…
//	POST /api/wechat/mp/callback
//
// 明文模式和安全模式（EncodingAESKey，AES-256-CBC）都支持：
// 请求带 <Encrypt> 就按安全模式校验 msg_signature、解密、加密回复。
//
// 回调还有个副作用很有用：谁给公众号发过消息，这里就能拿到他的 OpenID
// （模板消息必须按 OpenID 发），页面上直接填进接收人即可。

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

const wxMPAPI = "https://api.weixin.qq.com"

// WeChatMP 是服务号通道的运行态。
type WeChatMP struct {
	app *App

	mu        sync.Mutex
	client    *http.Client
	token     string
	tokenExp  time.Time
	lastErr   string
	lastErrAt time.Time
	lastOK    time.Time

	// 回调收到消息时记下发送者，页面上可以一键填入接收人
	lastOpenID   string
	lastOpenIDAt time.Time
}

// mpConfigured 判断推送所需四项是否齐全。
func (m *WeChatMP) mpConfigured() bool {
	s := m.app.Settings()
	return strings.TrimSpace(s.WeChatMPAppID) != "" &&
		strings.TrimSpace(s.WeChatMPSecret) != "" &&
		strings.TrimSpace(s.WeChatMPTemplateID) != "" &&
		len(mpOpenIDs(s.WeChatMPToUser)) > 0
}

func (m *WeChatMP) enabled() bool {
	return m.app.Settings().WeChatMPEnabled && m.mpConfigured()
}

// callbackReady 表示回调的 Token 已填（公众号后台校验要用）。
func (m *WeChatMP) callbackReady() bool {
	return strings.TrimSpace(m.app.Settings().WeChatMPToken) != ""
}

func (m *WeChatMP) httpClient() *http.Client {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.client == nil {
		// api.weixin.qq.com 国内直连可达，不走 TG 那个海外代理
		m.client, _ = newProxyClient("", 15*time.Second)
	}
	return m.client
}

func (m *WeChatMP) setErr(err error) {
	if err == nil {
		return
	}
	msg := m.scrub(err)
	m.mu.Lock()
	m.lastErr = msg
	m.lastErrAt = time.Now()
	m.mu.Unlock()
}

func (m *WeChatMP) setOK() {
	m.mu.Lock()
	m.lastOK = time.Now()
	m.lastErr = ""
	m.lastErrAt = time.Time{}
	m.mu.Unlock()
}

// scrub 把 Secret / token 从错误文本里抹掉（HTTP 报错会带整条 URL）。
func (m *WeChatMP) scrub(err error) string {
	if err == nil {
		return ""
	}
	s := m.app.Settings()
	msg := err.Error()
	if v := strings.TrimSpace(s.WeChatMPSecret); v != "" {
		msg = strings.ReplaceAll(msg, v, "<secret>")
	}
	m.mu.Lock()
	tok := m.token
	m.mu.Unlock()
	if tok != "" {
		msg = strings.ReplaceAll(msg, tok, "<token>")
	}
	return msg
}

// Notify 推送一条事件；没配置就静默跳过。
func (m *WeChatMP) Notify(level, message string) {
	if !m.enabled() {
		return
	}
	icon := map[string]string{
		"success": "✅", "info": "ℹ️", "warn": "⚠️", "error": "❌",
	}[level]
	if icon == "" {
		icon = "•"
	}
	if err := m.sendTemplate(icon + " " + message); err != nil {
		m.setErr(err)
		return
	}
	m.setOK()
}

// Test 同步发一条，错误原样返回给页面。
func (m *WeChatMP) Test() error {
	if !m.mpConfigured() {
		return fmt.Errorf("还没填齐服务号：AppID / AppSecret / 模板ID / 接收 OpenID")
	}
	if err := m.sendTemplate("✅ PanShare 测试消息：服务号通知链路正常"); err != nil {
		m.setErr(err)
		return err
	}
	m.setOK()
	return nil
}

// Status 给页面看的运行态。
func (m *WeChatMP) Status() map[string]any {
	s := m.app.Settings()
	m.mu.Lock()
	lastErr, errAt, okAt := m.lastErr, m.lastErrAt, m.lastOK
	openID, openAt := m.lastOpenID, m.lastOpenIDAt
	m.mu.Unlock()

	out := map[string]any{
		"mp_enabled":          m.enabled(),
		"mp_configured":       m.mpConfigured(),
		"mp_callback_ready":   m.callbackReady(),
		"mp_callback_url":     m.CallbackURL(),
		"mp_to_user":          strings.TrimSpace(s.WeChatMPToUser),
		"mp_last_error":       lastErr,
		"mp_last_openid":      openID,
		"mp_last_openid_from": mpStamp(openAt),
	}
	if !errAt.IsZero() {
		out["mp_last_error_at"] = mpStamp(errAt)
	}
	if !okAt.IsZero() {
		out["mp_last_ok_at"] = mpStamp(okAt)
	}
	return out
}

// CallbackURL 拼给公众号后台填的地址：公网地址 + 固定路径。
func (m *WeChatMP) CallbackURL() string {
	base := strings.TrimRight(strings.TrimSpace(m.app.Settings().WeChatMPBase), "/")
	if base == "" {
		base = m.app.BaseURL()
	}
	return base + "/api/wechat/mp/callback"
}

// ---------------------------------------------------------------- 模板消息

func (m *WeChatMP) accessToken(force bool) (string, error) {
	s := m.app.Settings()
	appID := strings.TrimSpace(s.WeChatMPAppID)
	secret := strings.TrimSpace(s.WeChatMPSecret)
	if appID == "" || secret == "" {
		return "", fmt.Errorf("还没填 AppID / AppSecret")
	}

	m.mu.Lock()
	if !force && m.token != "" && time.Now().Before(m.tokenExp) {
		tok := m.token
		m.mu.Unlock()
		return tok, nil
	}
	m.mu.Unlock()

	u := wxMPAPI + "/cgi-bin/token?grant_type=client_credential&appid=" +
		url.QueryEscape(appID) + "&secret=" + url.QueryEscape(secret)
	resp, err := m.httpClient().Get(u)
	if err != nil {
		return "", fmt.Errorf("连不上 api.weixin.qq.com：%v", m.scrub(err))
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))

	var r wxTokenResp
	if err := json.Unmarshal(body, &r); err != nil {
		return "", fmt.Errorf("微信返回看不懂：%s", snippet(body))
	}
	if r.ErrCode != 0 {
		return "", fmt.Errorf("取 access_token 失败：%d %s%s", r.ErrCode, r.ErrMsg, wxMPHint(r.ErrCode))
	}
	if r.AccessToken == "" {
		return "", fmt.Errorf("微信没有返回 access_token")
	}

	// 提前 5 分钟过期，避开边界
	ttl := time.Duration(r.ExpiresIn) * time.Second
	if ttl > 5*time.Minute {
		ttl -= 5 * time.Minute
	}
	m.mu.Lock()
	m.token = r.AccessToken
	m.tokenExp = time.Now().Add(ttl)
	m.mu.Unlock()
	return r.AccessToken, nil
}

func (m *WeChatMP) invalidateToken() {
	m.mu.Lock()
	m.token = ""
	m.tokenExp = time.Time{}
	m.mu.Unlock()
}

// sendTemplate 给每个 OpenID 发一条模板消息。
//
// 模板字段用 first / keyword1 / keyword2 / remark 这套最常见的三关键词模板；
// 微信会忽略模板里没有的字段，所以换模板也不用改代码。
// keyword2 装正文，按 200 字截断——模板字段普遍只有两三百字的空间，
// 超了整条会被拒，不如截断保证送达。
func (m *WeChatMP) sendTemplate(text string) error {
	s := m.app.Settings()
	tid := strings.TrimSpace(s.WeChatMPTemplateID)
	if tid == "" {
		return fmt.Errorf("还没填模板ID（公众号后台「模板消息」里的模板ID）")
	}
	ids := mpOpenIDs(s.WeChatMPToUser)
	if len(ids) == 0 {
		return fmt.Errorf("还没填接收 OpenID（让收件人给公众号发一条消息，页面会自动带出来）")
	}

	jump := strings.TrimRight(strings.TrimSpace(s.WeChatMPBase), "/")
	if jump == "" {
		jump = m.app.BaseURL()
	}

	lines := strings.Split(strings.TrimSpace(text), "\n")
	title := strings.TrimSpace(lines[0])
	if title == "" {
		title = "PanShare 通知"
	}
	body := strings.TrimSpace(text)
	if len(lines) > 1 {
		body = strings.Join(lines[1:], "\n")
	}
	body = truncateRunes(strings.TrimSpace(body), 200)
	if body == "" {
		body = title
	}

	var errs []string
	for _, openID := range ids {
		if err := m.sendOneTemplate(openID, tid, jump, truncateRunes(title, 100), body, false); err != nil {
			errs = append(errs, openID[:min(8, len(openID))]+"…："+err.Error())
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("%s", strings.Join(errs, "；"))
	}
	return nil
}

func (m *WeChatMP) sendOneTemplate(openID, tid, jump, title, body string, retried bool) error {
	tok, err := m.accessToken(false)
	if err != nil {
		return err
	}
	payload := map[string]any{
		"touser":      openID,
		"template_id": tid,
		"url":         jump,
		"data": map[string]any{
			"first":    map[string]string{"value": title, "color": "#173177"},
			"keyword1": map[string]string{"value": time.Now().Format("2006-01-02 15:04:05")},
			"keyword2": map[string]string{"value": body},
			"remark":   map[string]string{"value": "PanShare 自动通知"},
		},
	}
	b, _ := json.Marshal(payload)
	u := wxMPAPI + "/cgi-bin/message/template/send?access_token=" + url.QueryEscape(tok)
	resp, err := m.httpClient().Post(u, "application/json", bytes.NewReader(b))
	if err != nil {
		return fmt.Errorf("发送失败：%v", m.scrub(err))
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))

	var r wxSendResp
	if err := json.Unmarshal(rb, &r); err != nil {
		return fmt.Errorf("微信返回看不懂：%s", snippet(rb))
	}
	if !retried && (r.ErrCode == 40001 || r.ErrCode == 40014 || r.ErrCode == 42001) {
		m.invalidateToken()
		return m.sendOneTemplate(openID, tid, jump, title, body, true)
	}
	if r.ErrCode != 0 {
		return fmt.Errorf("%d %s%s", r.ErrCode, r.ErrMsg, wxMPHint(r.ErrCode))
	}
	return nil
}

// ---------------------------------------------------------------- 公网回调

// Verify 处理公众号后台保存配置时的 GET 校验，返回要原样回给微信的 echostr。
func (m *WeChatMP) Verify(q url.Values) (string, error) {
	s := m.app.Settings()
	token := strings.TrimSpace(s.WeChatMPToken)
	if token == "" {
		return "", fmt.Errorf("还没填回调 Token")
	}
	echo := q.Get("echostr")
	if !mpSignEqual(mpSign(token, q.Get("timestamp"), q.Get("nonce")), q.Get("signature")) {
		return "", fmt.Errorf("signature 校验不通过（公众号后台的 Token 与这里不一致）")
	}
	// 安全模式下 echostr 是加密的，要解密后回明文
	if q.Get("encrypt_type") == "aes" || q.Get("msg_signature") != "" {
		if key, err := mpAESKey(s.WeChatMPAESKey); err == nil {
			if plain, _, derr := mpDecrypt(key, echo); derr == nil {
				return plain, nil
			}
		}
	}
	if key, err := mpAESKey(s.WeChatMPAESKey); err == nil && echo != "" {
		if plain, _, derr := mpDecrypt(key, echo); derr == nil {
			return plain, nil
		}
	}
	return echo, nil
}

// Receive 处理公众号推来的消息，返回被动回复 XML（空串表示不回复）。
func (m *WeChatMP) Receive(q url.Values, body []byte) (string, error) {
	s := m.app.Settings()
	token := strings.TrimSpace(s.WeChatMPToken)
	if token == "" {
		return "", fmt.Errorf("还没填回调 Token")
	}

	var outer mpEnvelope
	if err := xml.Unmarshal(body, &outer); err != nil {
		return "", fmt.Errorf("回调 XML 解析失败：%v", err)
	}

	msgXML := body
	encrypted := strings.TrimSpace(outer.Encrypt) != ""
	if encrypted {
		key, err := mpAESKey(s.WeChatMPAESKey)
		if err != nil {
			return "", err
		}
		sig := mpSign(token, q.Get("timestamp"), q.Get("nonce"), outer.Encrypt)
		if !mpSignEqual(sig, q.Get("msg_signature")) {
			return "", fmt.Errorf("msg_signature 校验不通过")
		}
		plain, fromApp, err := mpDecrypt(key, outer.Encrypt)
		if err != nil {
			return "", err
		}
		if fromApp != "" && strings.TrimSpace(s.WeChatMPAppID) != "" && fromApp != strings.TrimSpace(s.WeChatMPAppID) {
			return "", fmt.Errorf("解密出的 AppID（%s）与配置的不一致", fromApp)
		}
		msgXML = []byte(plain)
	} else if q.Get("signature") != "" {
		// 明文模式也要验签：否则谁都能往这个地址推伪造消息
		sig := mpSign(token, q.Get("timestamp"), q.Get("nonce"))
		if !mpSignEqual(sig, q.Get("signature")) {
			return "", fmt.Errorf("signature 校验不通过")
		}
	}

	var msg mpMessage
	if err := xml.Unmarshal(msgXML, &msg); err != nil {
		return "", fmt.Errorf("消息 XML 解析失败：%v", err)
	}

	// 记下发送者，页面上可直接填成接收人
	if from := strings.TrimSpace(msg.FromUserName); strings.HasPrefix(from, "o") && from != strings.TrimSpace(msg.ToUserName) {
		m.mu.Lock()
		m.lastOpenID = from
		m.lastOpenIDAt = time.Now()
		m.mu.Unlock()
	}

	content := m.replyText(msg)
	if content == "" {
		return "", nil
	}
	reply := mpTextReplyXML(msg.FromUserName, msg.ToUserName, content)
	if !encrypted {
		return reply, nil
	}
	key, err := mpAESKey(s.WeChatMPAESKey)
	if err != nil {
		return "", err
	}
	enc, err := mpEncrypt(key, reply, strings.TrimSpace(s.WeChatMPAppID))
	if err != nil {
		return "", err
	}
	ts := fmt.Sprintf("%d", time.Now().Unix())
	nonce := mpNonce()
	return "<xml><Encrypt><![CDATA[" + enc + "]]></Encrypt><MsgSignature><![CDATA[" +
		mpSign(token, ts, nonce, enc) + "]]></MsgSignature><TimeStamp>" + ts +
		"</TimeStamp><Nonce><![CDATA[" + nonce + "]]></Nonce></xml>", nil
}

// replyText 决定回什么。公众号里回复「帮助」能看到说明，
// 其他文本一律回控制台地址——省得用户以为没人管。
func (m *WeChatMP) replyText(msg mpMessage) string {
	base := strings.TrimRight(m.CallbackBase(), "/")
	if base == "" {
		base = "（还没配公网地址）"
	}
	switch strings.ToLower(strings.TrimSpace(msg.MsgType)) {
	case "event":
		if strings.ToLower(msg.Event) == "subscribe" {
			return "欢迎关注 PanShare。\n控制台：" + base + "\n回复「帮助」查看说明。"
		}
		return ""
	case "text":
		text := strings.TrimSpace(msg.Content)
		low := strings.ToLower(text)
		if strings.Contains(low, "帮助") || low == "help" || low == "?" || low == "？" {
			return "PanShare 微信通道\n" +
				"· 任务开始 / 完成 / 失败会推到这里（模板消息）\n" +
				"· 控制台：" + base + "\n" +
				"· 你的 OpenID 已记录，可在「通知」页一键填成接收人"
		}
		return "已收到。控制台：" + base + "\n回复「帮助」查看说明。"
	}
	return ""
}

// CallbackBase 是给用户看的公网地址（协议 + 域名，不含回调路径）。
func (m *WeChatMP) CallbackBase() string {
	u := m.CallbackURL()
	return strings.TrimSuffix(u, "/api/wechat/mp/callback")
}

// LastOpenID 返回最近一次收到消息的发送者。
func (m *WeChatMP) LastOpenID() (string, time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.lastOpenID, m.lastOpenIDAt
}

// ---------------------------------------------------------------- 签名 / 加解密

type mpEnvelope struct {
	XMLName    xml.Name `xml:"xml"`
	ToUserName string   `xml:"ToUserName"`
	Encrypt    string   `xml:"Encrypt"`
}

type mpMessage struct {
	XMLName      xml.Name `xml:"xml"`
	ToUserName   string   `xml:"ToUserName"`
	FromUserName string   `xml:"FromUserName"`
	CreateTime   int64    `xml:"CreateTime"`
	MsgType      string   `xml:"MsgType"`
	Content      string   `xml:"Content"`
	Event        string   `xml:"Event"`
	EventKey     string   `xml:"EventKey"`
	MsgID        string   `xml:"MsgId"`
}

// mpSign 微信的签名规则：参数按字典序排好直接拼起来取 sha1。
func mpSign(parts ...string) string {
	cp := append([]string(nil), parts...)
	sort.Strings(cp)
	h := sha1.Sum([]byte(strings.Join(cp, "")))
	return hex.EncodeToString(h[:])
}

func mpSignEqual(a, b string) bool {
	a, b = strings.ToLower(strings.TrimSpace(a)), strings.ToLower(strings.TrimSpace(b))
	if a == "" || b == "" || len(a) != len(b) {
		return false
	}
	var diff byte
	for i := 0; i < len(a); i++ {
		diff |= a[i] ^ b[i]
	}
	return diff == 0
}

// mpAESKey 把 43 位 EncodingAESKey 还原成 32 字节 AES key。
func mpAESKey(s string) ([]byte, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil, fmt.Errorf("安全模式要填 EncodingAESKey（43 位）")
	}
	if len(s) != 43 {
		return nil, fmt.Errorf("EncodingAESKey 是 43 位字符，现在填了 %d 位", len(s))
	}
	key, err := base64.StdEncoding.DecodeString(s + "=")
	if err != nil || len(key) != 32 {
		return nil, fmt.Errorf("EncodingAESKey 解码失败，检查是否复制完整")
	}
	return key, nil
}

// mpDecrypt 解出明文与其中的 AppID。
// 明文结构：16 字节随机 + 4 字节消息长度（大端）+ 消息 + AppID。
func mpDecrypt(key []byte, cipherB64 string) (string, string, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(cipherB64))
	if err != nil {
		return "", "", fmt.Errorf("密文不是合法 base64：%v", err)
	}
	if len(raw) == 0 || len(raw)%aes.BlockSize != 0 {
		return "", "", fmt.Errorf("密文长度不对（%d）", len(raw))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", "", fmt.Errorf("AES 初始化失败：%v", err)
	}
	out := make([]byte, len(raw))
	cipher.NewCBCDecrypter(block, key[:aes.BlockSize]).CryptBlocks(out, raw)
	out, err = mpUnpad(out)
	if err != nil {
		return "", "", err
	}
	if len(out) < 20 {
		return "", "", fmt.Errorf("解密后内容太短")
	}
	n := int(binary.BigEndian.Uint32(out[16:20]))
	if n < 0 || 20+n > len(out) {
		return "", "", fmt.Errorf("解密后长度字段异常（%d）", n)
	}
	msg := string(out[20 : 20+n])
	appID := string(out[20+n:])
	return msg, appID, nil
}

// mpEncrypt 按安全模式加密（随机串 + 长度 + 消息 + AppID，PKCS#7 补位）。
func mpEncrypt(key []byte, msg, appID string) (string, error) {
	var rd [16]byte
	if _, err := rand.Read(rd[:]); err != nil {
		return "", err
	}
	buf := new(bytes.Buffer)
	buf.Write(rd[:])
	var lb [4]byte
	binary.BigEndian.PutUint32(lb[:], uint32(len(msg)))
	buf.Write(lb[:])
	buf.WriteString(msg)
	buf.WriteString(appID)

	plain := mpPad(buf.Bytes())
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", fmt.Errorf("AES 初始化失败：%v", err)
	}
	out := make([]byte, len(plain))
	cipher.NewCBCEncrypter(block, key[:aes.BlockSize]).CryptBlocks(out, plain)
	return base64.StdEncoding.EncodeToString(out), nil
}

func mpPad(b []byte) []byte {
	n := aes.BlockSize - len(b)%aes.BlockSize
	return append(b, bytes.Repeat([]byte{byte(n)}, n)...)
}

func mpUnpad(b []byte) ([]byte, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("解密内容为空")
	}
	n := int(b[len(b)-1])
	if n == 0 || n > aes.BlockSize || n > len(b) {
		return nil, fmt.Errorf("补位异常，检查 EncodingAESKey 是否正确")
	}
	return b[:len(b)-n], nil
}

func mpNonce() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}

// mpTextReplyXML 拼被动回复的文本消息。
func mpTextReplyXML(to, from, content string) string {
	content = strings.ReplaceAll(content, "]]>", "]]]]><![CDATA[>")
	return "<xml><ToUserName><![CDATA[" + to + "]]></ToUserName>" +
		"<FromUserName><![CDATA[" + from + "]]></FromUserName>" +
		"<CreateTime>" + fmt.Sprintf("%d", time.Now().Unix()) + "</CreateTime>" +
		"<MsgType><![CDATA[text]]></MsgType>" +
		"<Content><![CDATA[" + content + "]]></Content></xml>"
}

// mpOpenIDs 支持逗号 / 分号 / 空格分隔。
func mpOpenIDs(raw string) []string {
	fields := strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == '，' || r == ';' || r == '；' || r == ' ' || r == '\n' || r == '\r' || r == '\t'
	})
	out := make([]string, 0, len(fields))
	seen := map[string]bool{}
	for _, f := range fields {
		f = strings.TrimSpace(f)
		if f == "" || seen[f] {
			continue
		}
		seen[f] = true
		out = append(out, f)
	}
	return out
}

func mpStamp(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.Format("2006-01-02 15:04:05")
}

// wxMPHint 给服务号的常见错误码补人话。
func wxMPHint(code int) string {
	switch code {
	case 40001, 40014, 42001:
		return "（access_token 无效，多与 AppSecret 填错或被重置有关）"
	case 40013:
		return "（AppID 不对）"
	case 40125:
		return "（AppSecret 不对）"
	case 40164:
		return "（出口 IP 不在公众号后台的「IP 白名单」里，去「设置与开发 → 基本配置」加上服务器公网出口 IP）"
	case 40037:
		return "（模板ID 无效，检查是不是复制了模板标题而不是模板ID）"
	case 43004:
		return "（该 OpenID 还没关注这个公众号，先让 TA 关注再发）"
	case 45009:
		return "（接口调用超过频率限制）"
	case 48001:
		return "（这个公众号没有模板消息权限，订阅号 / 未认证服务号都用不了）"
	}
	return ""
}

// truncateRunes 按字符截断，避免把汉字切一半。
func truncateRunes(s string, n int) string {
	if n <= 0 {
		return ""
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}
