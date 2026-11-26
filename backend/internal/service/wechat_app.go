package service

import (
	"crypto/subtle"
	"encoding/xml"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"

	"panshare/internal/model"
)

// WeChatApp 处理企业微信「自建应用 → 接收消息」的回调。
//
// 存在的理由很具体：企业微信要求，配置「企业可信IP」之前必须先设好
// 可信域名或接收消息服务器 URL。可信域名要往站点根目录放验证文件，
// 而这条只要一个能通过校验的地址，顺便还能收到成员发给应用的消息。
//
// 协议和公众号安全模式一致：AES-256-CBC + PKCS#7，明文结构是
// 16 字节随机 + 4 字节长度（大端）+ 消息 + receiveid。差别只有两点：
// receiveid 是 CorpID，且 GET 校验时 echostr 也是加密的（要解出来再回）。
type WeChatApp struct {
	app *App
}

func (a *WeChatApp) settings() model.Settings { return a.app.Settings() }

// CallbackURL 是填进企业微信后台「接收消息 → 设置API接收」的地址。
// 配了 API 就把凭据挂在地址上，省得再去后台对 Token / EncodingAESKey。
func (a *WeChatApp) CallbackURL() string {
	base := strings.TrimRight(strings.TrimSpace(a.settings().WeChatMPBase), "/")
	if base == "" {
		base = a.app.BaseURL()
	}
	if base == "" {
		return ""
	}
	u := base + "/api/wechat/app/callback"
	if api := strings.TrimSpace(a.settings().WeChatAppAPI); api != "" {
		u += "?token=" + url.QueryEscape(api)
	}
	return u
}

// ready 表示回调地址已经可用：要么配了 API，要么 Token 与 EncodingAESKey 都填了。
func (a *WeChatApp) ready() bool {
	s := a.settings()
	if strings.TrimSpace(s.WeChatAppAPI) != "" {
		return true
	}
	return strings.TrimSpace(s.WeChatAppToken) != "" && strings.TrimSpace(s.WeChatAppAESKey) != ""
}

// trusted 判断请求带的 token 是否等于页面里生成的 API。
// 相等就意味着「地址本身」就是凭据，不再要求 msg_signature。
func (a *WeChatApp) trusted(q url.Values) bool {
	want := strings.TrimSpace(a.settings().WeChatAppAPI)
	if want == "" {
		return false
	}
	got := strings.TrimSpace(q.Get("token"))
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

func (a *WeChatApp) Status() map[string]any {
	s := a.settings()
	out := map[string]any{
		"app_callback_ready": a.ready(),
		"app_callback_url":   a.CallbackURL(),
		"commands":           s.WeChatCommands,
		"notify_kinds":       s.WeChatNotifyKinds,
	}
	if v := strings.TrimSpace(a.app.DB.GetMeta("wxapp_menu_at", "")); v != "" {
		out["menu_at"] = v
	}
	return out
}

// Verify 处理企业微信后台保存配置时的 GET 校验。
func (a *WeChatApp) Verify(q url.Values) (string, error) {
	s := a.settings()
	echo := strings.TrimSpace(q.Get("echostr"))
	if echo == "" {
		// 企业微信保存配置时一定带 echostr；没有它就是别人直接访问了这个地址
		// （浏览器打开、探活脚本），不是真校验失败，说清楚免得白折腾。
		return "", fmt.Errorf("这不是企业微信的校验请求：URL 里没有 echostr。" +
			"浏览器直接打开这个地址也会这样，属正常现象；请在企业微信后台点「保存」由它来请求")
	}

	// 地址里带了正确的 API：直接放行。
	// 企业微信后台的加解密方式选「明文模式」时 echostr 本身就是明文，原样回；
	// 万一后台选的是安全模式，就用库里的 AESKey 试着解一次，解不开再退回原文。
	if a.trusted(q) {
		if key, err := mpAESKey(s.WeChatAppAESKey); err == nil {
			if plain, _, derr := mpDecrypt(key, echo); derr == nil {
				return plain, nil
			}
		}
		return echo, nil
	}
	// 配了 API 却不匹配，直接说清楚——别再往下走旧的签名分支，
	// 那套提示里提到的 Token / EncodingAESKey 页面上已经没有了。
	if strings.TrimSpace(s.WeChatAppAPI) != "" {
		return "", fmt.Errorf("地址里的 token 不对：和页面「接收消息地址」里生成的 API 不一致" +
			"（复制时漏字或改了 API 没保存？）")
	}

	token := strings.TrimSpace(s.WeChatAppToken)
	if token == "" {
		return "", fmt.Errorf("还没填 API（页面「接收消息地址」里点「随机生成API」）")
	}
	sig := mpSign(token, q.Get("timestamp"), q.Get("nonce"), echo)
	if !mpSignEqual(sig, q.Get("msg_signature")) {
		return "", fmt.Errorf("msg_signature 校验不通过（Token 和企业微信后台填的不一致？）")
	}
	key, err := mpAESKey(s.WeChatAppAESKey)
	if err != nil {
		return "", err
	}
	plain, corp, err := mpDecrypt(key, echo)
	if err != nil {
		return "", err
	}
	if want := strings.TrimSpace(s.WeChatCorpID); want != "" && corp != "" && corp != want {
		return "", fmt.Errorf("解密出的 CorpID（%s）与配置的 CorpID（%s）不一致", corp, want)
	}
	return plain, nil
}

// wxAppEnvelope 是 POST 进来的加密信封。
type wxAppEnvelope struct {
	XMLName xml.Name `xml:"xml"`
	Encrypt string   `xml:"Encrypt"`
	AgentID string   `xml:"AgentID"`
}

// wxAppMessage 是解密之后的消息体。
type wxAppMessage struct {
	XMLName      xml.Name `xml:"xml"`
	ToUserName   string   `xml:"ToUserName"`
	FromUserName string   `xml:"FromUserName"`
	CreateTime   int64    `xml:"CreateTime"`
	MsgType      string   `xml:"MsgType"`
	Content      string   `xml:"Content"`
	Event        string   `xml:"Event"`
	EventKey     string   `xml:"EventKey"` // 点自定义菜单时是菜单的 key（即指令）
	MsgID        string   `xml:"MsgId"`
}

// Receive 处理成员发给应用的消息，返回已加密的回包（空串表示不回复）。
func (a *WeChatApp) Receive(q url.Values, body []byte) (string, error) {
	s := a.settings()
	if a.trusted(q) {
		return a.receiveTrusted(body, s)
	}
	if strings.TrimSpace(s.WeChatAppAPI) != "" {
		return "", fmt.Errorf("地址里的 token 不对：和页面里生成的 API 不一致")
	}
	token := strings.TrimSpace(s.WeChatAppToken)
	if token == "" {
		return "", fmt.Errorf("还没填 API（页面「接收消息地址」里点「随机生成API」）")
	}
	var env wxAppEnvelope
	if err := xml.Unmarshal(body, &env); err != nil {
		return "", fmt.Errorf("回调 XML 解析失败：%v", err)
	}
	enc := strings.TrimSpace(env.Encrypt)
	if enc == "" {
		return "", fmt.Errorf("请求体里没有 Encrypt（企业微信只支持安全模式）")
	}
	sig := mpSign(token, q.Get("timestamp"), q.Get("nonce"), enc)
	if !mpSignEqual(sig, q.Get("msg_signature")) {
		return "", fmt.Errorf("msg_signature 校验不通过")
	}
	key, err := mpAESKey(s.WeChatAppAESKey)
	if err != nil {
		return "", err
	}
	plain, corp, err := mpDecrypt(key, enc)
	if err != nil {
		return "", err
	}
	if want := strings.TrimSpace(s.WeChatCorpID); want != "" && corp != "" && corp != want {
		return "", fmt.Errorf("解密出的 CorpID（%s）与配置的不一致", corp)
	}

	var msg wxAppMessage
	if err := xml.Unmarshal([]byte(plain), &msg); err != nil {
		return "", fmt.Errorf("消息 XML 解析失败：%v", err)
	}

	content := a.replyText(msg)
	if content == "" {
		return "", nil
	}
	reply := mpTextReplyXML(msg.FromUserName, msg.ToUserName, content)

	encReply, err := mpEncrypt(key, reply, strings.TrimSpace(s.WeChatCorpID))
	if err != nil {
		return "", err
	}
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := mpNonce()
	return "<xml><Encrypt><![CDATA[" + encReply + "]]></Encrypt><MsgSignature><![CDATA[" +
		mpSign(token, ts, nonce, encReply) + "]]></MsgSignature><TimeStamp>" + ts +
		"</TimeStamp><Nonce><![CDATA[" + nonce + "]]></Nonce></xml>", nil
}

// receiveTrusted 处理地址里带对 API 的消息：先按加密信封解，
// 解不开就当明文 XML 处理（企业微信后台选「明文模式」时就是这样）。
func (a *WeChatApp) receiveTrusted(body []byte, s model.Settings) (string, error) {
	var env wxAppEnvelope
	if err := xml.Unmarshal(body, &env); err != nil {
		return "", fmt.Errorf("回调 XML 解析失败：%v", err)
	}
	enc := strings.TrimSpace(env.Encrypt)
	plain := string(body)
	if enc != "" {
		key, err := mpAESKey(s.WeChatAppAESKey)
		if err != nil {
			return "", err
		}
		p, _, derr := mpDecrypt(key, enc)
		if derr != nil {
			return "", derr
		}
		plain = p
	}

	var msg wxAppMessage
	if err := xml.Unmarshal([]byte(plain), &msg); err != nil {
		return "", fmt.Errorf("消息 XML 解析失败：%v", err)
	}
	content := a.replyText(msg)
	if content == "" {
		return "", nil
	}
	out := mpTextReplyXML(msg.FromUserName, msg.ToUserName, content)
	if enc == "" {
		return out, nil
	}

	key, err := mpAESKey(s.WeChatAppAESKey)
	if err != nil {
		return "", err
	}
	encReply, err := mpEncrypt(key, out, strings.TrimSpace(s.WeChatCorpID))
	if err != nil {
		return "", err
	}
	token := strings.TrimSpace(s.WeChatAppToken)
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := mpNonce()
	return "<xml><Encrypt><![CDATA[" + encReply + "]]></Encrypt><MsgSignature><![CDATA[" +
		mpSign(token, ts, nonce, encReply) + "]]></MsgSignature><TimeStamp>" + ts +
		"</TimeStamp><Nonce><![CDATA[" + nonce + "]]></Nonce></xml>", nil
}

// replyText 决定回什么内容。这里只做「链路已通」的确认，
// 真正的指令入口在 Telegram 那边。
func (a *WeChatApp) replyText(msg wxAppMessage) string {
	base := strings.TrimRight(strings.TrimSpace(a.settings().WeChatMPBase), "/")
	if base == "" {
		base = a.app.BaseURL()
	}
	if base == "" {
		base = "（还没填公网地址）"
	}
	switch strings.ToLower(strings.TrimSpace(msg.MsgType)) {
	case "event":
		switch strings.ToLower(strings.TrimSpace(msg.Event)) {
		case "enter_agent":
			return "PanShare 已就绪。\n控制台：" + base
		case "click":
			// 点的自定义菜单：key 就是指令，和聊天窗口里敲一样执行
			key := strings.TrimSpace(msg.EventKey)
			if key == "" {
				return ""
			}
			if !a.settings().WeChatCommands {
				return "指令没开：请到「通知」页打开「允许聊天窗口发指令 / 发链接」"
			}
			return cutReply(a.app.commandText(key))
		}
		return ""
	case "text":
		text := strings.TrimSpace(msg.Content)
		if text == "" {
			return ""
		}
		low := strings.ToLower(text)
		helpish := strings.Contains(low, "帮助") || low == "help" || text == "?" || text == "？"
		// 打开「允许发指令」后和 Telegram 走同一套：/help、/status、/inc、
		// 直接发 ed2k / 分享链接都行。普通聊天返回空串，不回复，免得刷屏。
		if a.settings().WeChatCommands {
			if helpish {
				text = "/help"
			}
			return cutReply(a.app.commandText(text))
		}
		if helpish {
			return "PanShare 企业微信通道\n" +
				"· 这个地址是「接收消息」回调，用来满足企业可信IP的前置条件\n" +
				"· 任务通知会主动推给你，不需要在这里发指令\n" +
				"· 想在聊天窗口发指令，请到「通知」页打开「允许聊天窗口发指令 / 发链接」\n" +
				"· 控制台：" + base
		}
		return "已收到。控制台：" + base
	}
	return ""
}

// wechatReplyLimit 是企业微信被动回复正文的实际上限（2048 字节），
// 留点余量给 XML 包装。
const wechatReplyLimit = 1800

// cutReply 按字节截断，保证不会把汉字切成半个。
func cutReply(s string) string {
	if len(s) <= wechatReplyLimit {
		return s
	}
	const tail = "\n…（内容太长已截断，完整内容看网页控制台）"
	limit := wechatReplyLimit - len(tail)
	var b strings.Builder
	for _, r := range s {
		if b.Len()+len(string(r)) > limit {
			break
		}
		b.WriteRune(r)
	}
	return strings.TrimRight(b.String(), "\n") + tail
}
