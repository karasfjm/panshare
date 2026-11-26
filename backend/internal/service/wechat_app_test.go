package service

import (
	"encoding/base64"
	"encoding/xml"
	"net/url"
	"strings"
	"testing"

	"panshare/internal/model"
)

func newTestWeChatApp(t *testing.T) (*WeChatApp, []byte) {
	t.Helper()
	raw := make([]byte, 32)
	for i := range raw {
		raw[i] = byte('a' + i)
	}
	aesKey43 := strings.TrimRight(base64.StdEncoding.EncodeToString(raw), "=")
	app := &App{settings: model.Settings{
		WeChatCorpID:    "wwcorp0001",
		WeChatAppToken:  "tokapp",
		WeChatAppAESKey: aesKey43,
		WeChatMPBase:    "https://ps.example.com",
	}}
	return &WeChatApp{app: app}, raw
}

// GET 校验：企业微信保存配置时会拿加密的 echostr 来敲门，要解密后回明文。
func TestWeChatAppVerify(t *testing.T) {
	m, raw := newTestWeChatApp(t)
	ts, nonce := "1700000000", "n1"
	const echoPlain = "echo-hello-123"

	encEcho, err := mpEncrypt(raw, echoPlain, "wwcorp0001")
	if err != nil {
		t.Fatal(err)
	}
	q := url.Values{
		"msg_signature": {mpSign("tokapp", ts, nonce, encEcho)},
		"timestamp":     {ts},
		"nonce":         {nonce},
		"echostr":       {encEcho},
	}
	got, err := m.Verify(q)
	if err != nil {
		t.Fatalf("校验应当通过：%v", err)
	}
	if got != echoPlain {
		t.Fatalf("应当回明文 %q，实际 %q", echoPlain, got)
	}

	// 签名不对必须拒绝，否则任何人都能探测
	bad := url.Values{
		"msg_signature": {"deadbeef"},
		"timestamp":     {ts},
		"nonce":         {nonce},
		"echostr":       {encEcho},
	}
	if _, err := m.Verify(bad); err == nil {
		t.Fatal("签名不对应当报错")
	}

	// 别的企业的 CorpID 加密的 echostr 不能放行
	encOther, _ := mpEncrypt(raw, echoPlain, "ww-other-corp")
	other := url.Values{
		"msg_signature": {mpSign("tokapp", ts, nonce, encOther)},
		"timestamp":     {ts},
		"nonce":         {nonce},
		"echostr":       {encOther},
	}
	if _, err := m.Verify(other); err == nil {
		t.Fatal("CorpID 不一致应当报错")
	}
}

// POST：解出成员消息，回包同样要加密且能被解开。
func TestWeChatAppReceive(t *testing.T) {
	m, raw := newTestWeChatApp(t)
	ts, nonce := "1700000001", "n2"

	msg := `<xml><ToUserName><![CDATA[wwcorp0001]]></ToUserName>` +
		`<FromUserName><![CDATA[zhangsan]]></FromUserName><CreateTime>1700000001</CreateTime>` +
		`<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[帮助]]></Content><MsgId>1</MsgId></xml>`
	enc, err := mpEncrypt(raw, msg, "wwcorp0001")
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`<xml><ToUserName><![CDATA[wwcorp0001]]></ToUserName>` +
		`<Encrypt><![CDATA[` + enc + `]]></Encrypt><AgentID>1000004</AgentID></xml>`)
	q := url.Values{
		"msg_signature": {mpSign("tokapp", ts, nonce, enc)},
		"timestamp":     {ts},
		"nonce":         {nonce},
	}

	reply, err := m.Receive(q, body)
	if err != nil {
		t.Fatalf("接收失败：%v", err)
	}
	var env struct {
		Encrypt      string `xml:"Encrypt"`
		MsgSignature string `xml:"MsgSignature"`
	}
	if err := xml.Unmarshal([]byte(reply), &env); err != nil || env.Encrypt == "" {
		t.Fatalf("回包不是加密信封：%s（%v）", reply, err)
	}
	plain, from, err := mpDecrypt(raw, env.Encrypt)
	if err != nil {
		t.Fatalf("回包解密失败：%v", err)
	}
	if from != "wwcorp0001" {
		t.Fatalf("回包的 receiveid 应当是 CorpID，实际 %q", from)
	}
	if !strings.Contains(plain, "企业微信通道") || !strings.Contains(plain, "https://ps.example.com") {
		t.Fatalf("回复内容不对：%s", plain)
	}
	if !strings.Contains(plain, "zhangsan") || !strings.Contains(plain, "<ToUserName") {
		t.Fatalf("回复应该发给发消息的人：%s", plain)
	}

	// 非文本事件默认不回复
	evt := `<xml><ToUserName><![CDATA[wwcorp0001]]></ToUserName><FromUserName><![CDATA[zhangsan]]></FromUserName>` +
		`<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[other_event]]></Event></xml>`
	encEvt, _ := mpEncrypt(raw, evt, "wwcorp0001")
	evtReply, err := m.Receive(url.Values{
		"msg_signature": {mpSign("tokapp", ts, nonce, encEvt)},
		"timestamp":     {ts},
		"nonce":         {nonce},
	}, []byte(`<xml><Encrypt><![CDATA[`+encEvt+`]]></Encrypt></xml>`))
	if err != nil {
		t.Fatalf("事件处理不该报错：%v", err)
	}
	if evtReply != "" {
		t.Fatalf("无关事件不该回复，实际：%s", evtReply)
	}
}

// 地址里带对 API 时不要求 msg_signature：明文模式的 echostr 原样回，
// 安全模式的密文 echostr 也要能解出来。
func TestWeChatAppVerifyByAPI(t *testing.T) {
	m, raw := newTestWeChatApp(t)
	m.app.settings.WeChatAppAPI = "api-abc123"

	got, err := m.Verify(url.Values{"token": {"api-abc123"}, "echostr": {"echo-plain"}})
	if err != nil {
		t.Fatalf("带对 API 应当放行：%v", err)
	}
	if got != "echo-plain" {
		t.Fatalf("明文模式应原样回 %q，实际 %q", "echo-plain", got)
	}

	encEcho, err := mpEncrypt(raw, "echo-secret", "wwcorp0001")
	if err != nil {
		t.Fatal(err)
	}
	got, err = m.Verify(url.Values{"token": {"api-abc123"}, "echostr": {encEcho}})
	if err != nil || got != "echo-secret" {
		t.Fatalf("安全模式的 echostr 应能解开：got=%q err=%v", got, err)
	}

	// API 不对就退回签名校验，没有签名必然拒绝
	if _, err := m.Verify(url.Values{"token": {"nope"}, "echostr": {"echo-plain"}}); err == nil {
		t.Fatal("API 不正确且无签名，应当拒绝")
	}
}

// 明文模式下的 POST：体里没有 Encrypt，直接回明文 XML。
func TestWeChatAppReceivePlainByAPI(t *testing.T) {
	m, _ := newTestWeChatApp(t)
	m.app.settings.WeChatAppAPI = "api-abc123"

	body := []byte(`<xml><ToUserName><![CDATA[wwcorp0001]]></ToUserName>` +
		`<FromUserName><![CDATA[zhangsan]]></FromUserName><CreateTime>1700000002</CreateTime>` +
		`<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[帮助]]></Content><MsgId>2</MsgId></xml>`)

	reply, err := m.Receive(url.Values{"token": {"api-abc123"}}, body)
	if err != nil {
		t.Fatalf("明文消息不该报错：%v", err)
	}
	if !strings.Contains(reply, "zhangsan") || !strings.Contains(reply, "企业微信通道") {
		t.Fatalf("回复内容不对：%s", reply)
	}
	if strings.Contains(reply, "<Encrypt>") {
		t.Fatalf("明文模式不该回加密信封：%s", reply)
	}
}

// 打开「允许聊天窗口发指令」后，微信里的消息走和 Telegram 同一套指令。
func TestWeChatAppCommands(t *testing.T) {
	m, _ := newTestWeChatApp(t)
	m.app.settings.WeChatAppAPI = "api-abc123"

	msg := func(s string) []byte {
		return []byte(`<xml><ToUserName><![CDATA[wwcorp0001]]></ToUserName>` +
			`<FromUserName><![CDATA[zhangsan]]></FromUserName><CreateTime>1</CreateTime>` +
			`<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[` + s + `]]></Content><MsgId>3</MsgId></xml>`)
	}
	ask := func(s string) (string, error) {
		return m.Receive(url.Values{"token": {"api-abc123"}}, msg(s))
	}

	// 默认关闭：只回一句「已收到」，不当指令执行
	reply, err := ask("/link")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reply, "已收到") {
		t.Fatalf("关闭指令时应只回执，实际：%s", reply)
	}

	m.app.settings.WeChatCommands = true

	// /link 走指令；联动删除总开关没开，应给出明确提示
	reply, err = ask("/link")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reply, "联动删除总开关没开") {
		t.Fatalf("指令没执行，实际：%s", reply)
	}

	// 未知指令
	reply, err = ask("/nope")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reply, "未知指令") {
		t.Fatalf("未知指令应有提示，实际：%s", reply)
	}

	// 普通聊天不回复
	reply, err = ask("今天天气不错")
	if err != nil {
		t.Fatal(err)
	}
	if reply != "" {
		t.Fatalf("普通聊天不该回复，实际：%s", reply)
	}
}

// 点自定义菜单：click 事件的 EventKey 就是指令，和聊天窗口敲一样执行。
func TestWeChatAppMenuClick(t *testing.T) {
	m, _ := newTestWeChatApp(t)
	m.app.settings.WeChatAppAPI = "api-abc123"
	m.app.settings.WeChatCommands = true

	body := []byte(`<xml><ToUserName><![CDATA[wwcorp0001]]></ToUserName>` +
		`<FromUserName><![CDATA[zhangsan]]></FromUserName><CreateTime>1</CreateTime>` +
		`<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[click]]></Event>` +
		`<EventKey><![CDATA[/nope]]></EventKey></xml>`)
	reply, err := m.Receive(url.Values{"token": {"api-abc123"}}, body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reply, "未知指令") {
		t.Fatalf("菜单点击应执行 key 里的指令，实际：%s", reply)
	}

	// 指令开关关掉时要说清原因，而不是静默
	m.app.settings.WeChatCommands = false
	reply, err = m.Receive(url.Values{"token": {"api-abc123"}}, body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reply, "指令没开") {
		t.Fatalf("关闭指令时应提示，实际：%s", reply)
	}
}

// 菜单结构要符合企业微信的限制：最多 3 个一级菜单，每个最多 5 个二级。
func TestWxAppMenuShape(t *testing.T) {
	m, ok := wxAppMenu(nil)["button"].([]any)
	if !ok || len(m) == 0 || len(m) > 3 {
		t.Fatalf("一级菜单数量不对：%d", len(m))
	}
	keys := 0
	for _, b := range m {
		btn, _ := b.(map[string]any)
		if _, has := btn["key"]; has {
			keys++
			continue
		}
		subs, _ := btn["sub_button"].([]any)
		if len(subs) == 0 || len(subs) > 5 {
			t.Fatalf("二级菜单数量不对：%d", len(subs))
		}
		for _, sb := range subs {
			sbm, _ := sb.(map[string]any)
			if sbm["type"] != "click" || sbm["key"] == "" {
				t.Fatalf("二级菜单必须是带 key 的 click：%v", sbm)
			}
			keys++
		}
	}
	if keys < 4 {
		t.Fatalf("菜单项太少：%d", keys)
	}
}

// 菜单按配置展开：帮助进「运行」，全量/增量各带 all + 每条配置。
func TestWxAppMenuWithConfigs(t *testing.T) {
	cfgs := []model.StrmConfig{
		{ID: "1", Name: "电影 Movie"},
		{ID: "2", Name: "纪录片"},
		{ID: "3", Name: "动画 Ani"},
	}
	btns, _ := wxAppMenu(cfgs)["button"].([]any)
	if len(btns) != 3 {
		t.Fatalf("应当正好 3 个一级菜单，实际 %d", len(btns))
	}

	names := make([]string, 0, 3)
	subs := make(map[string][]map[string]any, 3)
	for _, b := range btns {
		bm, _ := b.(map[string]any)
		name, _ := bm["name"].(string)
		names = append(names, name)
		list, _ := bm["sub_button"].([]any)
		if len(list) == 0 || len(list) > wxMenuMaxSub {
			t.Fatalf("%s 的二级菜单数量不对：%d", name, len(list))
		}
		for _, it := range list {
			im, _ := it.(map[string]any)
			if im["type"] != "click" || im["key"] == "" {
				t.Fatalf("%s 下的菜单项必须是带 key 的 click：%v", name, im)
			}
			subs[name] = append(subs[name], im)
		}
	}
	if strings.Join(names, ",") != "运行,全量,增量" {
		t.Fatalf("一级菜单应为 运行/全量/增量，实际 %v", names)
	}

	hasKey := func(name, key string) bool {
		for _, it := range subs[name] {
			if it["key"] == key {
				return true
			}
		}
		return false
	}
	if !hasKey("运行", "/help") || hasKey("全量", "/help") {
		t.Fatal("帮助应当挪进「运行」")
	}
	if !hasKey("全量", "/full all") || !hasKey("增量", "/inc all") {
		t.Fatal("全量/增量各自要有 all")
	}
	// 每条配置一条：key 走代号，名字用配置名
	if !hasKey("全量", "/full_movie") || !hasKey("增量", "/inc_movie") {
		t.Fatalf("缺配置专属菜单：%v", subs)
	}
	// 纯中文的「纪录片」拼不出代号，回落成 c2；带英文的用挑出来的字母
	if !hasKey("全量", "/full_c2") || !hasKey("增量", "/inc_c2") {
		t.Fatalf("纯中文配置名应当回落到 c2：%v", subs)
	}
	if !hasKey("增量", "/inc_ani") || !hasKey("全量", "/full_ani") {
		t.Fatalf("带英文的配置名应当用挑出来的字母：%v", subs)
	}
	// 配置名太长要截断，不能超企业微信长度限制
	if got := menuName("这是一个特别特别长的生成配置名字啊", "c9"); got != "这是一个特别特别长的生…" {
		t.Fatalf("长名字没按 12 字截断：%q", got)
	}
	if got := menuName("  ", "c9"); got != "c9" {
		t.Fatalf("空名字应回落到代号：%q", got)
	}

	// 配置超过 4 条时，多出来的不塞菜单（每家最多 5 个二级）
	many := make([]model.StrmConfig, 0, 8)
	for i := 0; i < 8; i++ {
		many = append(many, model.StrmConfig{ID: string(rune('a' + i)), Name: "配置" + string(rune('1'+i))})
	}
	bs, _ := wxAppMenu(many)["button"].([]any)
	for _, b := range bs {
		bm, _ := b.(map[string]any)
		if list, _ := bm["sub_button"].([]any); len(list) > wxMenuMaxSub {
			t.Fatalf("%s 超出 5 个二级菜单：%d", bm["name"], len(list))
		}
	}
}
