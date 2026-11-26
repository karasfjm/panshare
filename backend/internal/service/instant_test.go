package service

import (
	"strings"
	"testing"
)

func TestPickcodeOfPlayURL(t *testing.T) {
	cases := []struct {
		raw  string
		pick string
		ok   bool
	}{
		{"http://172.17.0.1:9527/p115/play/abr5dpfmgoajrhjhg/ZERO%20%E4%B8%80.mp4/2806858", "abr5dpfmgoajrhjhg", true},
		{"http://192.168.1.34:9527/p115/play/bf3amfc3ix8si84ha/SIVR-137-LTX.mp4/331466418", "bf3amfc3ix8si84ha", true},
		{"http://x:9527/s/swf1ztc3h3b_f5d0_2964965522465825442.mp4?/影片.mp4", "", false},
		{"http://x:9527/p115/play/", "", false},
		{"not a url", "", false},
	}
	for _, c := range cases {
		got, ok := pickcodeOfPlayURL(c.raw)
		if got != c.pick || ok != c.ok {
			t.Fatalf("pickcodeOfPlayURL(%q) = (%q,%v)，期望 (%q,%v)", c.raw, got, ok, c.pick, c.ok)
		}
	}
}

func TestRewritePlayURL(t *testing.T) {
	in := "http://172.17.0.1:9527/p115/play/abr5dpfmgoajrhjhg/ZERO%20%E4%B8%80%E8%8E%B7%E5%8D%83%E9%87%91%E6%B8%B8%E6%88%8F.mp4/2806858"
	got := rewritePlayURL(in, "evfxwndox2m0bzh62", "331466418")
	want := "http://172.17.0.1:9527/p115/play/evfxwndox2m0bzh62/ZERO%20%E4%B8%80%E8%8E%B7%E5%8D%83%E9%87%91%E6%B8%B8%E6%88%8F.mp4/331466418"
	if got != want {
		t.Fatalf("rewritePlayURL 得到\n  %s\n期望\n  %s", got, want)
	}
	// 改写后再取 pickcode 应该是新的
	if p, ok := pickcodeOfPlayURL(got); !ok || p != "evfxwndox2m0bzh62" {
		t.Fatalf("改写后 pickcode = %q, ok=%v", p, ok)
	}
	// 不是 play 地址时原样返回
	other := "http://x:9527/s/abc.mp4?/a.mp4"
	if rewritePlayURL(other, "p", "1") != other {
		t.Fatalf("非 play 地址不应被改写")
	}
}

func TestBuildInstantPath(t *testing.T) {
	in := "http://172.17.0.1:9527/p115/play/abr5dpfmgoajrhjhg/ZERO%20%E4%B8%80.mp4/2806858"
	got := buildInstantPath(in, "", "8CD1E7CA93A184220981203A5A0C47146A15E563",
		"ZERO 一获千金游戏 - S02E04 - 第4集.mp4", 412751677, "acc2", "acc0")
	want := "http://172.17.0.1:9527/p115/instant/8CD1E7CA93A184220981203A5A0C47146A15E563.mp4" +
		"?account=acc2&name=ZERO+%E4%B8%80%E8%8E%B7%E5%8D%83%E9%87%91%E6%B8%B8%E6%88%8F+-+S02E04+-+%E7%AC%AC4%E9%9B%86.mp4" +
		"&size=412751677&source=acc0"
	if got != want {
		t.Fatalf("buildInstantPath 得到\n  %s\n期望\n  %s", got, want)
	}

	// 相对地址：用 base 补全，否则 302 出去会被播放器解析到 Emby 域名
	rel := buildInstantPath("/p115/play/pc1/a.mkv/1", "http://192.168.1.34:9527/", "ABCDEF", "a.mkv", 100, "acc2", "")
	if !strings.HasPrefix(rel, "http://192.168.1.34:9527/p115/instant/ABCDEF.mkv?") {
		t.Fatalf("相对地址补全失败: %s", rel)
	}
	// 没有 base 又没 host：宁可返回空（不改写），也不要给出打不通的地址
	if buildInstantPath("/p115/play/pc1/a.mkv/1", "", "ABCDEF", "a.mkv", 100, "acc2", "") != "" {
		t.Fatalf("没有 base 时不该改写")
	}
}
