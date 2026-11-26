package core

import (
	"encoding/json"
	"strings"
	"testing"
)

func testStore(t *testing.T) *KV {
	t.Helper()
	return NewKV(t.TempDir())
}

func TestSettingsDefaults(t *testing.T) {
	st := testStore(t)
	s := Load(st)
	if s.Enabled || !s.Auto || !s.Notify || s.Interval != 300 {
		t.Fatalf("默认值不对：%+v", s)
	}
	if s.AgentIDs != "" || s.Proxy != "" {
		t.Fatalf("默认值不对：%+v", s)
	}
}

func TestSettingsPatch(t *testing.T) {
	st := testStore(t)
	// 前端经常把数字填成字符串（对齐 Python int(float(...)) 的宽容）
	got := Save(st, map[string]any{
		"enabled": true, "interval": "120", "agent_ids": " 1000001 ，abc ",
	})
	if !got.Enabled || got.Interval != 120 {
		t.Fatalf("patch 未生效：%+v", got)
	}
	if got.AgentIDs != "1000001 ，abc" {
		t.Fatalf("agent_ids 应只 trim 不重排：%q", got.AgentIDs)
	}
	// 往返
	if back := Load(st); back != got {
		t.Fatalf("往返不一致：%+v vs %+v", back, got)
	}
	// 没传的字段不动
	got2 := Save(st, map[string]any{"interval": 45})
	if got2.Interval != 60 { // 下限 60
		t.Fatalf("间隔下限应是 60：%d", got2.Interval)
	}
	if !got2.Enabled {
		t.Fatal("没传 enabled 不应被改")
	}
	if got2.AgentIDs != got.AgentIDs {
		t.Fatalf("没传 agent_ids 不应被改：%q", got2.AgentIDs)
	}
}

func TestSettingsBadJSON(t *testing.T) {
	st := testStore(t)
	_ = st.SetMeta(MetaKey, "{not json")
	if s := Load(st); s.Interval != 300 {
		t.Fatalf("坏 JSON 应回默认值：%+v", s)
	}
}

func TestAgentList(t *testing.T) {
	got := AgentList("1000001, 1000002；abc；，\n1000003\t999")
	want := []string{"1000001", "1000002", "1000003", "999"}
	if len(got) != len(want) {
		t.Fatalf("应解析出 %v：%v", want, got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("应解析出 %v：%v", want, got)
		}
	}
	if len(AgentList("abc,，；")) != 0 {
		t.Fatal("非数字段应被丢弃")
	}
}

func TestParseIPBody(t *testing.T) {
	cases := []struct{ svc, body, want string }{
		{"ipify", "203.0.113.7", "203.0.113.7"},
		{"ifconfig.me", "\n203.0.113.7\n", "203.0.113.7"},
		{"cloudflare", "h=cf\nip=198.51.100.9\nvisit_scheme=https", "198.51.100.9"},
		{"ipip.net", "当前 IP：192.0.2.66 来自于：中国", "192.0.2.66"},
		{"ip.sb", "not an ip here", ""},
		{"3322", "999.1.1.1", ""}, // 段超 255 不算 IP
		{"ipify", "", ""},
	}
	for _, c := range cases {
		if got := ParseIPBody(c.svc, c.body); got != c.want {
			t.Errorf("ParseIPBody(%s, %q) = %q, want %q", c.svc, c.body, got, c.want)
		}
	}
}

func TestHistory(t *testing.T) {
	st := testStore(t)
	if h := History(st); h != nil {
		t.Fatalf("空库应无历史：%v", h)
	}
	for i := 0; i < 25; i++ {
		PushHistory(st, map[string]any{"at": float64(i), "ip": "10.0.0.1"})
	}
	h := History(st)
	if len(h) != 20 {
		t.Fatalf("历史应截到 20 条：%d", len(h))
	}
	if h[0]["at"].(float64) != 5 { // 最早的 5 条被截掉
		t.Fatalf("应保留最新的 20 条：%v", h[0])
	}
}

func TestAppendIP(t *testing.T) {
	if got := appendIP("", "203.0.113.7"); got != "203.0.113.7" {
		t.Fatalf("空列表应直接加：%q", got)
	}
	if got := appendIP("198.51.100.1;198.51.100.2", "203.0.113.7"); got != "198.51.100.1;198.51.100.2;203.0.113.7" {
		t.Fatalf("应追加不覆盖：%q", got)
	}
	if got := appendIP("198.51.100.1；203.0.113.7，x", "203.0.113.7"); got != "198.51.100.1；203.0.113.7，x" {
		t.Fatalf("已有该 IP 应原样返回：%q", got)
	}
	// 超过 50 条裁旧的
	var items []string
	for i := 0; i < 55; i++ {
		items = append(items, "10.0.0."+string(rune('a'+i%26))+itoa(i))
	}
	got := appendIP(strings.Join(items, ";"), "203.0.113.7")
	if got := strings.Split(got, ";"); len(got) != 50 {
		t.Fatalf("应裁到 50 条：%d", len(got))
	}
}

func itoa(n int) string {
	raw, _ := json.Marshal(n)
	return string(raw)
}

func TestChromiumAvailable(t *testing.T) {
	// 不应 panic；本机有没有浏览器都行，只验证函数能跑
	_ = ChromiumAvailable()
	_ = FindChromium()
}

func TestVerifyCodeFlow(t *testing.T) {
	m := New(t.TempDir(), testStore(t), nil)

	// 空验证码被拒
	if r := m.RequestVerifyCode("   "); r["ok"] != false {
		t.Fatalf("空验证码应被拒：%+v", r)
	}

	// 提交后能取一次，且只取一次（消费掉）
	if r := m.RequestVerifyCode("  123456  "); r["ok"] != true {
		t.Fatalf("提交应成功：%+v", r)
	}
	if got := m.takeVerifyCode(); got != "123456" {
		t.Fatalf("应取到 trim 后的 code：%q", got)
	}
	if got := m.takeVerifyCode(); got != "" {
		t.Fatalf("验证码应只被消费一次：%q", got)
	}

	// setVerify 切到 verify 状态并带上截图/提示，Status 能读到
	m.setVerify("data:image/png;base64,AAAA", "请查收短信")
	if m.loginState != LoginVerify {
		t.Fatalf("状态应是 verify：%s", m.loginState)
	}
	st := m.Status()
	if st["verify"] != "data:image/png;base64,AAAA" || st["verify_msg"] != "请查收短信" {
		t.Fatalf("Status 未带验证码信息：%+v", st)
	}

	// clearVerify 清空截图和待提交 code
	_ = m.RequestVerifyCode("999")
	m.clearVerify()
	if m.verifyB64 != "" || m.verifyCode != "" || m.verifyMsg != "" {
		t.Fatalf("clearVerify 未清空：img=%q code=%q msg=%q", m.verifyB64, m.verifyCode, m.verifyMsg)
	}
}

func TestBrowserStatusInState(t *testing.T) {
	m := New(t.TempDir(), testStore(t), nil)
	m.setBrowser(BrowserInstalling, "正在下载浏览器…")
	st := m.Status()
	if st["browser"] != BrowserInstalling {
		t.Fatalf("browser 状态未输出：%+v", st["browser"])
	}
	if st["browser_msg"] != "正在下载浏览器…" {
		t.Fatalf("browser_msg 未输出：%+v", st["browser_msg"])
	}
	if st["browser_at"] == nil || st["browser_at"].(float64) <= 0 {
		t.Fatalf("browser_at 应是时间戳：%+v", st["browser_at"])
	}
	m.setBrowser(BrowserReady, "浏览器已就绪")
	if m.Status()["browser"] != BrowserReady {
		t.Fatal("browser 状态未更新为 ready")
	}
}
