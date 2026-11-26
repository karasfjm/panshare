package main

import "testing"

// 回归：家庭云列表里的 familyId 是 JSON 数字，firstOf 用 %v 会给出
// "5.79739066e+08"，拿这个去请求 listFiles 会被判成
// InternalError familyStatusCheck()（看着像没权限，其实是 id 被写坏了）。
func TestNumStrKeepsFamilyIDInteger(t *testing.T) {
	m := map[string]any{
		"familyId": float64(579739066),
		"name":     "我的家庭云",
	}
	if got := numStr(m, "familyId"); got != "579739066" {
		t.Fatalf("familyId=%q，期望 579739066", got)
	}
	if got := firstOf(m, "familyId"); got == "579739066" {
		t.Fatal("样本已经不会被格式化成科学计数法，测试失去意义")
	}
	// 字符串形态原样返回
	if got := numStr(map[string]any{"familyId": " 12345 "}, "familyId"); got != "12345" {
		t.Fatalf("字符串 id 被改了：%q", got)
	}
	if got := numStr(map[string]any{}, "familyId"); got != "" {
		t.Fatalf("缺字段应为空，得到 %q", got)
	}
}

func TestFixNumRepairsOldRefs(t *testing.T) {
	cases := map[string]string{
		"5.79739066e+08": "579739066",
		"579739066":      "579739066",
		"abc123":         "abc123",
		"":               "",
	}
	for in, want := range cases {
		if got := fixNum(in); got != want {
			t.Fatalf("fixNum(%q)=%q，期望 %q", in, got, want)
		}
	}
}

func TestParseRefFamily(t *testing.T) {
	scope, fam, folder := parseRef("fam:5.79739066e+08")
	if scope != "family" {
		t.Fatalf("scope=%q", scope)
	}
	if fam != "579739066" {
		t.Fatalf("老配置里的科学计数法 id 没修好：%q", fam)
	}
	// 家庭云根目录的 folderID 留空，listFiles 会补成 -11
	if folder != "" {
		t.Fatalf("家庭云根目录的 folderID 应为空，得到 %q", folder)
	}
	if s, f, fo := parseRef(""); s != "personal" || f != "" || fo != rootFolderID {
		t.Fatalf("个人云根解析错误：%q %q %q", s, f, fo)
	}
	if s, _, _ := parseRef("fam"); s != "family_list" {
		t.Fatalf("fam 应解析成家庭云列表，得到 %q", s)
	}
}

func TestPlayURL(t *testing.T) {
	base, prefix := "https://ps.example.com:28676/", "/p189/"
	got := playURL(base, prefix, "c1", "", "123", "危险.mkv")
	want := "https://ps.example.com:28676/p189/play/c1/123/%E5%8D%B1%E9%99%A9.mkv"
	if got != want {
		t.Fatalf("个人云地址\n got %s\nwant %s", got, want)
	}
	got = playURL(base, prefix, "c1", "579739066", "123", "危险.mkv")
	want = "https://ps.example.com:28676/p189/play/c1/fam/579739066/123/%E5%8D%B1%E9%99%A9.mkv"
	if got != want {
		t.Fatalf("家庭云地址\n got %s\nwant %s", got, want)
	}
	// 没有文件名时退回老格式
	if got := playURL("http://h", "/p", "c1", "", "123", ""); got != "http://h/p/play/c1/123" {
		t.Fatalf("老格式地址不对：%s", got)
	}
}
