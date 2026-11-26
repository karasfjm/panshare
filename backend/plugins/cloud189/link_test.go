package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// resolveFinalLink 要把「下载入口」跟一跳换成 CDN 真实地址；
// 不跳转时原样返回，别把地址搞丢。
func TestResolveFinalLink(t *testing.T) {
	final := "https://cdn.example.com/vod/a.mp4?t=1"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("User-Agent"); got != browserUA {
			t.Errorf("跟跳时 UA 不对：%s", got)
		}
		http.Redirect(w, r, final, http.StatusFound)
	}))
	defer srv.Close()

	if got := resolveFinalLink(srv.URL); got != final {
		t.Fatalf("跟跳结果 %q，期望 %q", got, final)
	}
}

func TestResolveFinalLinkKeepsRawWhenNoRedirect(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	defer srv.Close()

	if got := resolveFinalLink(srv.URL); got != srv.URL {
		t.Fatalf("没有跳转时应返回原地址，得到 %q", got)
	}
	if got := resolveFinalLink(""); got != "" {
		t.Fatalf("空地址应原样返回，得到 %q", got)
	}
}
