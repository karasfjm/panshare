package service

import (
	"strings"
	"testing"

	"panshare/internal/model"
)

func TestSplitByBytes(t *testing.T) {
	// 小于上限：原样返回一条
	got := splitByBytes("hello\nworld", 2000)
	if len(got) != 1 || got[0] != "hello\nworld" {
		t.Fatalf("短文本不该被切分，got=%q", got)
	}

	// 空文本：不产生消息（避免给微信发空串被拒）
	if got := splitByBytes("   \n\n ", 2000); len(got) != 0 {
		t.Fatalf("空文本应返回空，got=%q", got)
	}

	// 多行超限：应切成多条，且每条都不超过上限，拼起来内容不丢
	var lines []string
	for i := 0; i < 100; i++ {
		lines = append(lines, strings.Repeat("行", 20)) // 每行 40 字节
	}
	text := strings.Join(lines, "\n")
	chunks := splitByBytes(text, 200)
	if len(chunks) < 2 {
		t.Fatalf("超限文本应被切分，got %d 条", len(chunks))
	}
	total := 0
	for _, c := range chunks {
		if n := len([]byte(c)); n > 200 {
			t.Fatalf("分片超过上限：%d 字节", n)
		}
		total += strings.Count(c, "行")
	}
	if total != 100*20 {
		t.Fatalf("分片后内容丢失：期望 %d 个「行」，实际 %d", 100*20, total)
	}

	// 单行本身超限：按 rune 硬切，不能切出半个汉字
	long := strings.Repeat("长", 300) // 900 字节
	one := splitByBytes(long, 100)
	joined := ""
	for _, c := range one {
		if len([]byte(c)) > 100 {
			t.Fatalf("单行分片超过上限：%d 字节", len([]byte(c)))
		}
		if !strings.HasPrefix(long, c) && !strings.Contains(long, c) {
			t.Fatalf("分片不是原文子串（可能切坏了 UTF-8）：%q", c)
		}
		joined += c
	}
	if joined != long {
		t.Fatalf("硬切后内容不一致")
	}
}

func TestWxHint(t *testing.T) {
	if !strings.Contains(wxHint(60020), "可信 IP") {
		t.Fatalf("60020 应提示可信 IP，got=%q", wxHint(60020))
	}
	if wxHint(0) != "" {
		t.Fatalf("未知错误码不该有提示")
	}
}

// 通知类型过滤：留空=全部，「-」=都不推，逗号列表按类型匹配。
func TestWeChatWantsKinds(t *testing.T) {
	app := &App{}
	w := &WeChat{app: app}
	cases := []struct {
		list string
		kind string
		want bool
	}{
		{"", "strm", true},
		{"", "anything", true},
		{"-", "strm", false},
		{"strm,share", "share", true},
		{"strm,share", "plugin", false},
		{" strm , share ", "share", true},
	}
	for _, c := range cases {
		app.settings = model.Settings{WeChatNotifyKinds: c.list}
		if got := w.wantsKinds(c.kind); got != c.want {
			t.Fatalf("list=%q kind=%q 期望 %v，实际 %v", c.list, c.kind, c.want, got)
		}
	}
}
