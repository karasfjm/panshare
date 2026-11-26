package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"io"
	"strings"
	"testing"
	"time"

	"panshare/internal/config"
	"panshare/internal/model"
)

func TestMediaItem(t *testing.T) {
	cases := []struct {
		path string
		item string
		ok   bool
	}{
		{"/videos/0f1e2d/stream", "0f1e2d", true},
		{"/Videos/0f1e2d/original.mp4", "0f1e2d", true},
		{"/audio/0f1e2d/stream", "0f1e2d", true},
		{"/items/0f1e2d/download", "0f1e2d", true},
		{"/items/0f1e2d/file", "0f1e2d", true},
		{"/sync/jobitems/0f1e2d/file", "0f1e2d", true},
		{"/videos/0f1e2d/subtitles", "", false},
		{"/videos/0f1e2d/subtitles/0/Stream.srt", "", false},
		{"/items/0f1e2d/images/primary", "", false},
		{"/emby/videos/0f1e2d/stream", "", false}, // 前缀由调用方先剥掉
		{"/users/0f1e2d/items", "", false},
	}
	for _, c := range cases {
		item, _, ok := mediaItem(c.path)
		if ok != c.ok || item != c.item {
			t.Errorf("mediaItem(%q) = (%q,%v)，期望 (%q,%v)", c.path, item, ok, c.item, c.ok)
		}
	}
}

func TestPathItem(t *testing.T) {
	for _, c := range []struct{ in, want string }{
		{"/items/abc/playbackinfo", "abc"},
		{"/emby/items/abc/PlaybackInfo", "abc"},
		{"/Items/abc/PlaybackInfo", "abc"},
	} {
		if got := pathItem(c.in); got != c.want {
			t.Errorf("pathItem(%q)=%q，期望 %q", c.in, got, c.want)
		}
	}
}

func TestMayBeHTMLShell(t *testing.T) {
	for _, p := range []string{"/", "/web/index.html", "/web/", "/emby/web/index.html"} {
		if !mayBeHTMLShell(p) {
			t.Errorf("%q 应该按 HTML 壳处理", p)
		}
	}
	for _, p := range []string{"/videos/a/stream", "/items/a/download", "/emby/web/modules/x.js", "/web/modules/app.js"} {
		if mayBeHTMLShell(p) {
			t.Errorf("%q 不该整包读进内存", p)
		}
	}
}

func TestAPIKeyOf(t *testing.T) {
	r := httptest.NewRequest("GET", "/videos/x/stream?X-Emby-Token=qtok", nil)
	if got := apiKeyOf(r, "fallback"); got != "qtok" {
		t.Errorf("query token 取错：%q", got)
	}
	r = httptest.NewRequest("GET", "/videos/x/stream", nil)
	r.Header.Set("Authorization", `MediaBrowser Client="Emby Web", Device="Chrome", Token="htok"`)
	if got := apiKeyOf(r, "fallback"); got != "htok" {
		t.Errorf("Authorization token 取错：%q", got)
	}
	r = httptest.NewRequest("GET", "/videos/x/stream", nil)
	if got := apiKeyOf(r, "fallback"); got != "fallback" {
		t.Errorf("没有 token 时应回落到服务器 Key：%q", got)
	}
}

// newTestProxy 组一个不依赖数据库的反代：目标直接写死成 fake Emby。
func newTestProxy(t *testing.T, upstream string) *EmbyProxy {
	t.Helper()
	app := &App{Cfg: &config.Config{EmbyProxyPort: 7259}}
	p := NewEmbyProxy(app)
	p.target = upstream
	p.targetAt = time.Now()
	return p
}

// TestPlaybackInfoAnd302 串起来验证：PlaybackInfo 强制 DirectPlay → 媒体路由 302 到最终直链。
func TestPlaybackInfoAnd302(t *testing.T) {
	media := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/file.mp4":
			http.Redirect(w, r, "/real.mp4?sign=1", http.StatusFound)
		case "/real.mp4":
			w.Header().Set("Content-Length", "100")
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer media.Close()

	emby := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/Items/abc/PlaybackInfo" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"MediaSources": []any{map[string]any{
				"Id": "ms1", "Path": media.URL + "/file.mp4",
				"IsRemote": true, "Protocol": "Http", "Type": "Video",
				"SupportsTranscoding": true,
				"TranscodingUrl":      "/videos/abc/master.m3u8",
			}},
		})
	}))
	defer emby.Close()

	p := newTestProxy(t, emby.URL)
	front := httptest.NewServer(p)
	defer front.Close()

	client := &http.Client{
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}

	// 1. PlaybackInfo 应被改写
	resp, err := client.Post(front.URL+"/Items/abc/PlaybackInfo?X-Emby-Token=tok", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	var data map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	src := data["MediaSources"].([]any)[0].(map[string]any)
	if src["SupportsTranscoding"] != false || src["SupportsDirectPlay"] != true {
		t.Fatalf("没有强制 DirectPlay：%v", src)
	}
	if _, ok := src["TranscodingUrl"]; ok {
		t.Fatalf("TranscodingUrl 应该被删掉：%v", src)
	}
	want := "/videos/abc/stream?Static=true&MediaSourceId=ms1"
	if src["DirectStreamUrl"] != want {
		t.Fatalf("DirectStreamUrl=%v，期望 %v", src["DirectStreamUrl"], want)
	}

	// 2. 媒体路由应 302 到解析后的最终地址
	req, _ := http.NewRequest(http.MethodGet, front.URL+"/videos/abc/stream?Static=true&MediaSourceId=ms1&X-Emby-Token=tok", nil)
	req.Header.Set("User-Agent", "Infuse/7.0")
	resp, err = client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusFound {
		t.Fatalf("status=%d，期望 302", resp.StatusCode)
	}
	if loc := resp.Header.Get("Location"); loc != media.URL+"/real.mp4?sign=1" {
		t.Fatalf("Location=%q，期望 %q", loc, media.URL+"/real.mp4?sign=1")
	}
}

// TestProxyPortReload 验证页面上改端口后能在原地重绑监听。
func TestProxyPortReload(t *testing.T) {
	emby := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("ok"))
	}))
	defer emby.Close()

	p := newTestProxy(t, emby.URL)
	freePort := func() int {
		l, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer l.Close()
		return l.Addr().(*net.TCPAddr).Port
	}
	first, second := freePort(), freePort()
	p.app.settingsMu.Lock()
	p.app.settings = model.Settings{EmbyProxyPort: first}
	p.app.settingsMu.Unlock()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go p.Start(ctx)

	waitPort := func(port int) {
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			if p.Running() && p.ListenPort() == port {
				return
			}
			time.Sleep(20 * time.Millisecond)
		}
		t.Fatalf("等 %d 端口监听超时（当前 %d，running=%v）", port, p.ListenPort(), p.Running())
	}
	waitPort(first)

	resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/", first))
	if err != nil {
		t.Fatalf("首端口请求失败：%v", err)
	}
	resp.Body.Close()

	// 改成第二个端口并唤醒监听循环
	p.app.settingsMu.Lock()
	p.app.settings.EmbyProxyPort = second
	p.app.settingsMu.Unlock()
	p.Reload()
	waitPort(second)

	if _, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/", first)); err == nil {
		t.Fatal("老端口应该已经关掉了")
	}
	resp, err = http.Get(fmt.Sprintf("http://127.0.0.1:%d/", second))
	if err != nil {
		t.Fatalf("新端口请求失败：%v", err)
	}
	resp.Body.Close()
	if p.EffectivePort() != second {
		t.Fatalf("EffectivePort=%d，期望 %d", p.EffectivePort(), second)
	}
}

// TestHTMLInjection 验证 HTML 壳里被塞了 crossOrigin 拦截脚本。
func TestHTMLInjection(t *testing.T) {
	emby := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte("<html><head><title>Emby</title></head><body>hi</body></html>"))
	}))
	defer emby.Close()

	p := newTestProxy(t, emby.URL)
	front := httptest.NewServer(p)
	defer front.Close()

	resp, err := http.Get(front.URL + "/web/index.html")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf, _ := io.ReadAll(resp.Body)
	body := string(buf)
	if !strings.Contains(body, crossOriginMarker) {
		t.Fatalf("HTML 里没有注入脚本：%s", body)
	}
	if !strings.Contains(body, "<title>Emby</title>") {
		t.Fatalf("HTML 正文被破坏了：%s", body)
	}
}

// TestSystemInfoPortRewrite 验证端口被改写成反代端口。
func TestSystemInfoPortRewrite(t *testing.T) {
	emby := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ServerName":"e","Version":"4.8","WebSocketPortNumber":8096,"HttpServerPortNumber":8096,"LocalAddress":"http://192.168.1.10:8096"}`))
	}))
	defer emby.Close()

	p := newTestProxy(t, emby.URL)
	front := httptest.NewServer(p)
	defer front.Close()

	resp, err := http.Get(front.URL + "/System/Info")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var data map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		t.Fatal(err)
	}
	// 客户端连的是 front 的端口，System/Info 必须把它写成「连回去」的端口
	frontPort, ok := hostPort(strings.TrimPrefix(front.URL, "http://"))
	if !ok {
		t.Fatalf("解析测试端口失败：%s", front.URL)
	}
	if data["WebSocketPortNumber"] != float64(frontPort) {
		t.Fatalf("WebSocketPortNumber=%v，期望 %d", data["WebSocketPortNumber"], frontPort)
	}
	if data["LocalAddress"] != "http://192.168.1.10:"+itoa(frontPort) {
		t.Fatalf("LocalAddress=%v", data["LocalAddress"])
	}
}
