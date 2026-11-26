package service

import (
	"testing"

	"panshare/internal/model"
)

func TestCmdSlug(t *testing.T) {
	cases := map[string]string{
		"电影":                "",          // 纯中文挑不出字符，调用方会退回 c1/c2
		"Movies 4K":          "movies4k",  // 空格去掉
		"12 月新番":             "c12",       // 数字开头补 c
		"a-very-long-config-name": "averylongcon", // 截到 12 位
		"":                   "",
	}
	for in, want := range cases {
		if got := cmdSlug(in); got != want {
			t.Errorf("cmdSlug(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestConfigAliases(t *testing.T) {
	cfgs := []model.StrmConfig{
		{ID: "1", Name: "电影"},
		{ID: "2", Name: "剧集"},
		{ID: "3", Name: "Movies"},
		{ID: "4", Name: "movies"}, // 与上面同代号，要补序号
		{ID: "5", Name: "all"},    // 撞保留字，也要补
	}
	got := configAliases(cfgs)
	want := map[string]string{"1": "c1", "2": "c2", "3": "movies", "4": "movies2", "5": "all2"}
	for id, w := range want {
		if got[id] != w {
			t.Errorf("配置 %s 的代号 = %q, want %q", id, got[id], w)
		}
	}
}

func TestExtractLinks(t *testing.T) {
	text := `来两条：
ed2k://|file|A.mkv|1234|ABCDEF0123456789ABCDEF0123456789|/
https://115.com/s/swabc123?password=x1y2#，这个是分享
再补一条 ed2k://|file|B.mkv|5678|0123456789ABCDEF0123456789ABCDEF|/
ed2k://|file|A.mkv|1234|ABCDEF0123456789ABCDEF0123456789|/`
	ed2ks, shares := extractLinks(text)
	if len(ed2ks) != 2 {
		t.Fatalf("ed2k 应该去重后剩 2 条，实际 %d：%v", len(ed2ks), ed2ks)
	}
	if len(shares) != 1 {
		t.Fatalf("分享应该有 1 条，实际 %d：%v", len(shares), shares)
	}
	// 尾巴上的中文标点必须被削掉，否则 ParseShare 解析失败
	if shares[0] != "https://115.com/s/swabc123?password=x1y2#" {
		t.Errorf("分享链接没清干净：%q", shares[0])
	}
}

func TestExtractLinksIgnorePlainChat(t *testing.T) {
	ed2ks, shares := extractLinks("今天更新了吗？")
	if len(ed2ks) != 0 || len(shares) != 0 {
		t.Fatalf("普通聊天不该提取出链接：%v %v", ed2ks, shares)
	}
}
