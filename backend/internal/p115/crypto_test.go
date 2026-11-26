package p115

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestCleanUID(t *testing.T) {
	cases := map[string]string{
		"331466418":              "331466418",
		"2806858_R1_1794486118":  "2806858",
		"A2806858_R1_1794486118": "2806858",
		" 1794486118 ":           "1794486118",
		"":                       "",
		"abc":                    "",
	}
	for in, want := range cases {
		if got := CleanUID(in); got != want {
			t.Errorf("CleanUID(%q)=%q，期望 %q", in, got, want)
		}
	}
}

// 失败响应（大文件常见的 50028）不能误判成「有直链」，且错误信息要带错误码，
// 这样页面上能直接看到原因，而不是笼统一句「没有返回下载地址」。
func TestParseDownloadRejects(t *testing.T) {
	bad, _ := json.Marshal(map[string]any{"state": false, "msg_code": 50028,
		"msg": "文件大小超出限制，请使用115电脑端下载"})
	if u, _ := parseDownload(bad, nil); u != "" {
		t.Fatalf("失败响应不该解出链接: %q", u)
	}
	if r := reasonOf(bad); !strings.Contains(r, "50028") || !strings.Contains(r, "超出限制") {
		t.Fatalf("原因应包含错误码和原文，得到 %q", r)
	}
}

// 明文响应（旧接口/兜底路径）要能直接取到 file_url。
func TestParseDownloadPlain(t *testing.T) {
	body, _ := json.Marshal(map[string]any{
		"state":    true,
		"file_url": "https://cdnfhnfile.115.com/a.mkv?t=1&f=3",
	})
	u, _ := parseDownload(body, nil)
	if u != "https://cdnfhnfile.115.com/a.mkv?t=1&f=3" {
		t.Fatalf("没取到 file_url，得到 %q", u)
	}
}

// f=3 的直链播放时要带上响应 Set-Cookie 里的 cookie，这里验一下是否被带上。
func TestMergeHeaderCookie(t *testing.T) {
	hdr := http.Header{}
	hdr.Add("Set-Cookie", "acw_tc=abc123; Path=/; Max-Age=3600")
	hdr.Add("Set-Cookie", "cdn_key=zzz; Path=/")
	got := mergeHeader(nil, map[string]any{}, hdr)
	if got["Cookie"] != "acw_tc=abc123; cdn_key=zzz" {
		t.Fatalf("cookie 合并结果不对: %q", got["Cookie"])
	}
}
