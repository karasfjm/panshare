package service

import (
	"encoding/base64"
	"encoding/xml"
	"net/url"
	"strings"
	"testing"

	"panshare/internal/model"
)

// 微信签名与参数顺序无关（内部会排序）。
func TestMPSignOrderIndependent(t *testing.T) {
	a := mpSign("tok", "1234567890", "nonceabc")
	b := mpSign("nonceabc", "tok", "1234567890")
	if a != b {
		t.Fatalf("签名应与会话参数顺序无关：%s != %s", a, b)
	}
	if len(a) != 40 {
		t.Fatalf("sha1 十六进制应为 40 位，得到 %d", len(a))
	}
	if mpSignEqual(a, strings.ToUpper(a)) != true {
		t.Fatal("签名比较应忽略大小写")
	}
	if mpSignEqual(a, "deadbeef") {
		t.Fatal("不同签名不应判为相等")
	}
}

// 安全模式的加解密必须能原样还原，且带出 AppID。
func TestMPEncryptDecryptRoundTrip(t *testing.T) {
	key := []byte("0123456789abcdef0123456789abcdef")
	msg := "<xml><Content><![CDATA[任务完成 ✅]]></Content></xml>"
	enc, err := mpEncrypt(key, msg, "wx1234567890abcdef")
	if err != nil {
		t.Fatalf("加密失败：%v", err)
	}
	if _, err := base64.StdEncoding.DecodeString(enc); err != nil {
		t.Fatalf("密文应为 base64：%v", err)
	}
	plain, appID, err := mpDecrypt(key, enc)
	if err != nil {
		t.Fatalf("解密失败：%v", err)
	}
	if plain != msg {
		t.Fatalf("明文不一致：%q != %q", plain, msg)
	}
	if appID != "wx1234567890abcdef" {
		t.Fatalf("AppID 不一致：%q", appID)
	}
}

// 换 key 解不出来，不能悄悄成功。
func TestMPDecryptWrongKey(t *testing.T) {
	enc, err := mpEncrypt([]byte("0123456789abcdef0123456789abcdef"), "hello", "wxapp")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := mpDecrypt([]byte("ffffffffffffffffffffffffffffffff"), enc); err == nil {
		t.Fatal("用错 key 应该解密失败")
	}
}

// EncodingAESKey 必须是 43 位，解码出 32 字节。
func TestMPAESKey(t *testing.T) {
	raw := make([]byte, 32)
	for i := range raw {
		raw[i] = byte('A' + i%26)
	}
	enc := base64.StdEncoding.EncodeToString(raw)
	key43 := strings.TrimRight(enc, "=")
	if len(key43) != 43 {
		t.Fatalf("测试用 key 长度应为 43，实际 %d", len(key43))
	}
	got, err := mpAESKey(key43)
	if err != nil {
		t.Fatalf("解析 EncodingAESKey 失败：%v", err)
	}
	if string(got) != string(raw) {
		t.Fatal("解出的 key 与原始不一致")
	}
	if _, err := mpAESKey("short"); err == nil {
		t.Fatal("长度不对应当报错")
	}
}

// OpenID 支持逗号 / 分号 / 空格分隔，并去重。
func TestMPOpenIDs(t *testing.T) {
	got := mpOpenIDs(" oAAA, oBBB；oAAA oCCC\noDDD ")
	want := []string{"oAAA", "oBBB", "oCCC", "oDDD"}
	if len(got) != len(want) {
		t.Fatalf("解析结果数量不对：%v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("第 %d 个不一致：%q != %q", i, got[i], want[i])
		}
	}
	if len(mpOpenIDs("   ")) != 0 {
		t.Fatal("空串应返回空切片")
	}
}

func TestTruncateRunes(t *testing.T) {
	if got := truncateRunes("一二三四五", 3); got != "一二三…" {
		t.Fatalf("按字符截断失败：%q", got)
	}
	if got := truncateRunes("短", 10); got != "短" {
		t.Fatalf("未超长不该改动：%q", got)
	}
}

// 回复 XML 里的 CDATA 结束符要转义，否则整条 XML 会坏掉。
func TestMPTextReplyXML(t *testing.T) {
	out := mpTextReplyXML("oUser", "gh_abc", "含 ]]> 的文本")
	if !strings.Contains(out, "<ToUserName><![CDATA[oUser]]></ToUserName>") {
		t.Fatalf("接收方没写进去：%s", out)
	}
	if strings.Count(out, "]]>") < 1 {
		t.Fatal("CDATA 结构被破坏")
	}
	if strings.Contains(out, "含 ]]> 的") {
		t.Fatal("CDATA 结束符没转义")
	}
}

// 回调的完整链路：GET 验签、明文 POST、安全模式 POST（含加密回复）。
func TestWeChatMPCallbackFlow(t *testing.T) {
	raw := make([]byte, 32)
	for i := range raw {
		raw[i] = byte('a' + i)
	}
	aesKey43 := strings.TrimRight(base64.StdEncoding.EncodeToString(raw), "=")
	app := &App{settings: model.Settings{
		WeChatMPToken:  "tok123",
		WeChatMPAESKey: aesKey43,
		WeChatMPAppID:  "wxappid0001",
		WeChatMPBase:   "https://ps.example.com",
	}}
	m := &WeChatMP{app: app}

	ts, nonce := "1700000000", "nonce42"

	// 1) 公众号后台保存配置时的 GET 校验
	q := url.Values{
		"signature": {mpSign("tok123", ts, nonce)},
		"timestamp": {ts},
		"nonce":     {nonce},
		"echostr":   {"echo-plain-123"},
	}
	echo, err := m.Verify(q)
	if err != nil || echo != "echo-plain-123" {
		t.Fatalf("GET 校验失败：echo=%q err=%v", echo, err)
	}

	// 验签错就该拒绝，不能放行
	bad := url.Values{
		"signature": {"deadbeef"},
		"timestamp": {ts},
		"nonce":     {nonce},
		"echostr":   {"x"},
	}
	if _, err := m.Verify(bad); err == nil {
		t.Fatal("签名不对应当报错")
	}

	// 2) 明文模式的文本消息
	body := `<xml><ToUserName><![CDATA[gh_1]]></ToUserName>` +
		`<FromUserName><![CDATA[oUser123abc]]></FromUserName><CreateTime>1700000001</CreateTime>` +
		`<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[帮助]]></Content><MsgId>1</MsgId></xml>`
	reply, err := m.Receive(url.Values{}, []byte(body))
	if err != nil {
		t.Fatalf("明文消息处理失败：%v", err)
	}
	if !strings.Contains(reply, "微信通道") || !strings.Contains(reply, "https://ps.example.com") {
		t.Fatalf("回复内容不对：%s", reply)
	}
	if id, _ := m.LastOpenID(); id != "oUser123abc" {
		t.Fatalf("没记住发送者 OpenID：%q", id)
	}

	// 明文模式带 signature 时也要验签，防止伪造推送
	okQ := url.Values{
		"signature": {mpSign("tok123", ts, nonce)},
		"timestamp": {ts},
		"nonce":     {nonce},
	}
	if _, err := m.Receive(okQ, []byte(body)); err != nil {
		t.Fatalf("明文模式正常验签不该失败：%v", err)
	}
	badQ := url.Values{
		"signature": {"deadbeef"},
		"timestamp": {ts},
		"nonce":     {nonce},
	}
	if _, err := m.Receive(badQ, []byte(body)); err == nil {
		t.Fatal("明文模式签名不对应当报错")
	}

	// 3) 安全模式：请求加密、回复也必须加密
	enc, err := mpEncrypt(raw, body, "wxappid0001")
	if err != nil {
		t.Fatal(err)
	}
	q2 := url.Values{
		"msg_signature": {mpSign("tok123", ts, nonce, enc)},
		"timestamp":     {ts},
		"nonce":         {nonce},
	}
	reply2, err := m.Receive(q2, []byte(`<xml><Encrypt><![CDATA[`+enc+`]]></Encrypt></xml>`))
	if err != nil {
		t.Fatalf("安全模式处理失败：%v", err)
	}
	var env mpEnvelope
	if err := xml.Unmarshal([]byte(reply2), &env); err != nil || env.Encrypt == "" {
		t.Fatalf("回复不是加密信封：%s（%v）", reply2, err)
	}
	plain, _, err := mpDecrypt(raw, env.Encrypt)
	if err != nil {
		t.Fatalf("回复解密失败：%v", err)
	}
	if !strings.Contains(plain, "微信通道") {
		t.Fatalf("加密回复内容不对：%s", plain)
	}

	// 4) AppID 对不上时必须拒绝，避免别人的公众号往这推
	encOther, _ := mpEncrypt(raw, body, "wx-other-app")
	q3 := url.Values{
		"msg_signature": {mpSign("tok123", ts, nonce, encOther)},
		"timestamp":     {ts},
		"nonce":         {nonce},
	}
	if _, err := m.Receive(q3, []byte(`<xml><Encrypt><![CDATA[`+encOther+`]]></Encrypt></xml>`)); err == nil {
		t.Fatal("AppID 不一致应当报错")
	}
}
