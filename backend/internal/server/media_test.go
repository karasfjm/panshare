package server

import "testing"

func TestNeedRelay(t *testing.T) {
	const ua = "Hills/1.11.0 (android; 13)"
	cases := []struct {
		link   string
		header map[string]string
		ua     string
		want   bool
	}{
		{"https://cdn.115.com/a.mkv?t=1&f=1", nil, ua, false},
		{"https://cdn.115.com/a.mkv?t=1&f=3", nil, ua, true},
		{"https://cdn.115.com/a.mkv?f=3&t=1", nil, ua, true},
		{"https://cdn.115.com/a.mkv?f=31", nil, ua, false},
		// 只有 acw_tc（WAF cookie）时不该中转，f 才是判定依据
		{"https://cdn.115.com/a.mkv", map[string]string{"Cookie": "acw_tc=1"}, ua, false},
		{"https://cdn.115.com/a.mkv?f=3", map[string]string{"Cookie": "acw_tc=1"}, ua, true},
		// 客户端不发 UA：f=1 的直链 302 过去会被 CDN 回 403，只能中转
		{"https://cdn.115.com/a.mkv?t=1&f=1", nil, "", true},
		{"https://cdn.115.com/a.mkv?t=1&f=1", nil, "   ", true},
	}
	for _, c := range cases {
		if got := needRelay(c.link, c.header, c.ua); got != c.want {
			t.Errorf("needRelay(%q, ua=%q)=%v，期望 %v", c.link, c.ua, got, c.want)
		}
	}
}

func TestMediaType(t *testing.T) {
	cases := map[string]string{
		"https://cdn.115.com/a/b.mkv?t=1":  "video/x-matroska",
		"https://cdn.115.com/a/b.MP4":      "video/mp4",
		"https://cdn.115.com/a/b.ts":       "video/mp2t",
		"https://cdn.115.com/a/b.nfo":      "text/plain; charset=utf-8",
		"https://cdn.115.com/a/unknown.xyz": "",
	}
	for link, want := range cases {
		if got := mediaType(link); got != want {
			t.Errorf("mediaType(%q)=%q，期望 %q", link, got, want)
		}
	}
}
