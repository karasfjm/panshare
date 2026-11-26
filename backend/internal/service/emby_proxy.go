package service

// Emby 反代（独立端口）。思路参考 DDSRem-Dev/MoviePilot-Plugins 的
// embyreverseproxy 插件，Go 版做了等价实现：
//
//  1. 媒体路由（/videos/{id}/stream、/audio/...、/items/{id}/download ...）
//     先问 Emby 要 MediaSource.Path，拿到 strm 里的 http 地址后 HEAD 跟一跳，
//     把最终的 115 直链直接 302 给播放器 —— 视频流量不经过本服务。
//  2. /Items/{id}/PlaybackInfo 若命中 strm 源，强制 DirectPlay/DirectStream、
//     关掉转码，并把 DirectStreamUrl 改成指向本反代的相对路径，
//     否则浏览器会按编码能力走 HLS 转码，302 永远不生效。
//  3. HTML 壳与 htmlvideoplayer 的 js 里去掉 crossOrigin：
//     302 跳到 115 CDN 是跨域的，浏览器一旦带上 crossOrigin="anonymous"
//     就会按 CORS 校验，115 不回 ACAO，播放器直接报错。
//  4. /System/Info 把 WebSocketPort/HttpPort 改成本反代端口，客户端才会连回代理。
//  5. 其余请求（含 WebSocket 升级）原样透传。

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	proxyURLCacheTTL  = 90 * time.Second
	proxyStrmCacheTTL = 5 * time.Minute
	proxyUserCacheTTL = 5 * time.Minute

	crossOriginMarker = "[PanShare] crossOrigin"
)

// 这些「名字」看着像媒体，其实是附属信息，不能拿去 302。
var nonMediaNames = map[string]bool{
	"additionalparts": true,
	"subtitles":       true,
	"similar":         true,
	"thememedia":      true,
	"themevideos":     true,
	"themesongs":      true,
	"specialfeatures": true,
	"linkeditems":     true,
}

// 注入到 Emby Web 的 HTML 壳里：把 video/audio 的 crossOrigin 属性废掉。
const crossOriginScript = `<script>
(function(){
  // ` + crossOriginMarker + ` 拦截器 — 302 直链播放时不要触发 CORS
  try {
    Object.defineProperty(HTMLMediaElement.prototype,'crossOrigin',{
      get:function(){return null},
      set:function(){},
      configurable:true
    });
  } catch(e){}
  try {
    var ob=new MutationObserver(function(ms){
      ms.forEach(function(m){
        if(m.type==='attributes'&&m.attributeName==='crossorigin'){m.target.removeAttribute('crossorigin')}
        if(m.type==='childList'){m.addedNodes.forEach(function(n){
          if(n.nodeType===1&&(n.tagName==='VIDEO'||n.tagName==='AUDIO')){n.removeAttribute('crossorigin')}
        })}
      });
    });
    ob.observe(document.documentElement,{attributes:true,attributeFilter:['crossorigin'],childList:true,subtree:true});
  } catch(e){}
})();
</script>`

var (
	// basehtmlplayer.js: getCrossOriginValue 里的 `"anonymous"` 三元表达式
	baseCrossOriginRe = regexp.MustCompile(`\w+\.IsRemote\s*&&\s*"DirectPlay"\s*===\s*\w+\s*\?\s*null\s*:\s*"anonymous"`)
	// plugin.js: `&&(elem.crossOrigin=initialSubtitleStream)`
	pluginCrossOriginRe = regexp.MustCompile(`&&\s*\(\w+\.crossOrigin\s*=\s*\w+\)`)
)

type proxyURLEntry struct {
	url    string
	expire time.Time
}

type proxyStrmEntry struct {
	sources map[string]string
	expire  time.Time
}

type proxyUserEntry struct {
	uid    string
	expire time.Time
}

// EmbyProxy 是跑在独立端口上的 Emby 反代。
type EmbyProxy struct {
	app *App

	mu       sync.Mutex
	target   string
	targetAt time.Time

	cacheMu sync.Mutex
	urls    map[string]proxyURLEntry
	strms   map[string]proxyStrmEntry
	users   map[string]proxyUserEntry

	transport *http.Transport
	follow    *http.Client // 跟随重定向，用来 HEAD 解析最终直链

	running   atomic.Bool
	lastErr   atomic.Value // string
	started   time.Time
	requests  atomic.Int64
	redirects atomic.Int64

	// 端口热切换：页面上改了端口 → Reload() 唤醒监听循环重绑
	portChange chan struct{}
	curPort    atomic.Int32
}

func NewEmbyProxy(a *App) *EmbyProxy {
	tr := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:          64,
		MaxIdleConnsPerHost:   32,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
		ExpectContinueTimeout: time.Second,
		// 自己去处理压缩头：要改写的响应必须拿到未压缩正文
		DisableCompression: true,
		ForceAttemptHTTP2:  false,
	}
	return &EmbyProxy{
		app:        a,
		urls:       map[string]proxyURLEntry{},
		strms:      map[string]proxyStrmEntry{},
		users:      map[string]proxyUserEntry{},
		transport:  tr,
		follow:     &http.Client{Transport: tr},
		portChange: make(chan struct{}, 1),
	}
}

// EffectivePort 返回当前生效的监听端口：页面手填优先，否则用环境变量默认值。
// 0 表示关闭（页面上的开关关掉，或端口为 0）。
func (p *EmbyProxy) EffectivePort() int {
	st := p.app.Settings()
	if st.EmbyProxyOff {
		return 0
	}
	if st.EmbyProxyPort > 0 {
		return st.EmbyProxyPort
	}
	return p.app.Cfg.EmbyProxyPort
}

// Reload 让监听循环重新读端口（设置里改完端口后调用）。
func (p *EmbyProxy) Reload() {
	select {
	case p.portChange <- struct{}{}:
	default:
	}
}

// InvalidateTarget 在 Emby 服务器登记变化时清掉目标缓存。
func (p *EmbyProxy) InvalidateTarget() {
	p.mu.Lock()
	p.target = ""
	p.targetAt = time.Time{}
	p.mu.Unlock()
}

// Running 报告监听是否已经跑起来。
func (p *EmbyProxy) Running() bool { return p.running.Load() }

// ListenPort 返回真实在监听的端口（重绑过程中可能是 0）。
func (p *EmbyProxy) ListenPort() int { return int(p.curPort.Load()) }

// LastError 返回最近一次启动/监听错误。
func (p *EmbyProxy) LastError() string {
	if v, ok := p.lastErr.Load().(string); ok {
		return v
	}
	return ""
}

// Stats 给前端显示用。
func (p *EmbyProxy) Stats() map[string]any {
	p.mu.Lock()
	target := p.target
	p.mu.Unlock()
	if target == "" {
		target = p.upstream()
	}
	return map[string]any{
		"enabled":    p.EffectivePort() > 0,
		"on":         !p.app.Settings().EmbyProxyOff,
		"port":       p.EffectivePort(),
		"listen_port": p.ListenPort(),
		"port_default": p.app.Cfg.EmbyProxyPort,
		"port_custom":  p.app.Settings().EmbyProxyPort,
		"running":    p.running.Load(),
		"target":     target,
		"last_error": p.LastError(),
		"uptime":     proxyUptime(p.started),
		"requests":   p.requests.Load(),
		"redirects":  p.redirects.Load(),
	}
}

func proxyUptime(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return time.Since(t).Round(time.Second).String()
}

// Start 在生效端口上监听；页面改端口后会自动重绑，不用重启容器。
func (p *EmbyProxy) Start(ctx context.Context) {
	p.started = time.Now()
	for {
		port := p.EffectivePort()
		if port <= 0 {
			p.running.Store(false)
			p.curPort.Store(0)
			log.Printf("Emby 反代：端口为 0（PS_EMBY_PROXY_PORT=%d，页面设置=%d），未启动",
				p.app.Cfg.EmbyProxyPort, p.app.Settings().EmbyProxyPort)
			if !p.waitReload(ctx, 0) {
				return
			}
			continue
		}

		ln, err := net.Listen("tcp", fmt.Sprintf(":%d", port))
		if err != nil {
			p.running.Store(false)
			p.curPort.Store(0)
			p.lastErr.Store(err.Error())
			log.Printf("Emby 反代监听 %d 失败：%v", port, err)
			p.app.Event("warn", "emby", fmt.Sprintf("Emby 反代端口 %d 绑定失败：%v", port, err), "")
			// 端口先被别的进程占着：过 30 秒再试，别刷屏
			if !p.waitReload(ctx, 30*time.Second) {
				return
			}
			continue
		}

		srv := &http.Server{
			Handler:           p,
			ReadHeaderTimeout: 20 * time.Second,
			IdleTimeout:       120 * time.Second,
			// 不设 WriteTimeout：一条视频流可能持续几个小时
		}
		p.curPort.Store(int32(port))
		p.running.Store(true)
		p.lastErr.Store("")
		p.started = time.Now()
		log.Printf("Emby 反代已启动：:%d → %s", port, p.upstream())

		done := make(chan struct{})
		go func() {
			defer close(done)
			if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
				p.lastErr.Store(err.Error())
				log.Printf("Emby 反代服务异常：%v", err)
			}
		}()

		select {
		case <-ctx.Done():
			p.shutdown(srv)
			<-done
			return
		case <-p.portChange:
			log.Printf("Emby 反代：端口有变化，重绑监听")
			p.shutdown(srv)
			<-done
		}
	}
}

func (p *EmbyProxy) shutdown(srv *http.Server) {
	p.running.Store(false)
	p.curPort.Store(0)
	sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(sctx)
}

// waitReload 等一次 Reload 或超时；返回 false 表示 ctx 已结束。
func (p *EmbyProxy) waitReload(ctx context.Context, d time.Duration) bool {
	var timeout <-chan time.Time
	if d > 0 {
		t := time.NewTimer(d)
		defer t.Stop()
		timeout = t.C
	}
	select {
	case <-ctx.Done():
		return false
	case <-p.portChange:
		return true
	case <-timeout:
		return true
	}
}

// upstream 返回第一台启用的 Emby 服务器，缓存 5 秒。
func (p *EmbyProxy) upstream() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.target != "" && time.Since(p.targetAt) < 5*time.Second {
		return p.target
	}
	if p.app == nil || p.app.DB == nil {
		return p.target
	}
	srv := p.app.UpstreamEmby()
	if srv == nil {
		p.target = ""
		p.targetAt = time.Now()
		return ""
	}
	p.target = strings.TrimRight(srv.URL, "/")
	p.targetAt = time.Now()
	return p.target
}

func (p *EmbyProxy) upstreamKey() string {
	if p.app == nil || p.app.DB == nil {
		return ""
	}
	if srv := p.app.UpstreamEmby(); srv != nil {
		return srv.APIKey
	}
	return ""
}

// ---------------------------------------------------------------- 路由

func (p *EmbyProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	p.requests.Add(1)
	up := p.upstream()
	if up == "" {
		http.Error(w, "还没有启用的 Emby 服务器，请先到「媒体库」添加", http.StatusBadGateway)
		return
	}
	rel := strings.TrimPrefix(r.URL.Path, "/emby")
	if rel == "" {
		rel = "/"
	}

	if r.Method == http.MethodOptions {
		if _, _, ok := mediaItem(rel); ok {
			writeCORS(w)
			w.WriteHeader(http.StatusNoContent)
			return
		}
	}

	switch {
	case isPlaybackInfo(rel):
		p.serve(w, r, up, p.rewritePlaybackInfo)
	case isSystemInfo(rel):
		p.serve(w, r, up, p.rewriteSystemInfo)
	default:
		if item, _, ok := mediaItem(rel); ok && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			if p.redirectMedia(w, r, up, item) {
				return
			}
		}
		p.serve(w, r, up, p.rewriteStatic)
	}
}

// mediaItem 判断是不是媒体直取路由，返回条目 id。
func mediaItem(rel string) (item, name string, ok bool) {
	seg := strings.Split(strings.Trim(rel, "/"), "/")
	if len(seg) < 3 {
		return "", "", false
	}
	switch strings.ToLower(seg[0]) {
	case "audio", "videos":
		if len(seg) != 3 {
			return "", "", false
		}
		if nonMediaNames[strings.ToLower(seg[2])] {
			return "", "", false
		}
		return seg[1], seg[2], true
	case "items":
		if len(seg) != 3 {
			return "", "", false
		}
		last := strings.ToLower(seg[2])
		if last != "download" && last != "file" {
			return "", "", false
		}
		return seg[1], last, true
	case "sync":
		if len(seg) != 4 || strings.ToLower(seg[1]) != "jobitems" || strings.ToLower(seg[3]) != "file" {
			return "", "", false
		}
		return seg[2], "file", true
	}
	return "", "", false
}

func isPlaybackInfo(rel string) bool {
	seg := strings.Split(strings.Trim(rel, "/"), "/")
	if len(seg) != 3 {
		return false
	}
	return strings.EqualFold(seg[0], "items") && strings.EqualFold(seg[2], "playbackinfo")
}

func isSystemInfo(rel string) bool {
	seg := strings.Split(strings.Trim(rel, "/"), "/")
	return len(seg) == 2 && strings.EqualFold(seg[0], "system") && strings.EqualFold(seg[1], "info")
}

// ---------------------------------------------------------------- 302

func (p *EmbyProxy) redirectMedia(w http.ResponseWriter, r *http.Request, up, item string) bool {
	msID := r.URL.Query().Get("MediaSourceId")
	key := strings.Join([]string{item, msID, p.userOf(r, item), headerHash(r)}, "|")

	if final, ok := p.getURL(key); ok {
		p.answerRedirect(w, r, item, final, "缓存")
		return true
	}

	var candidate string
	if tok := apiKeyOf(r, p.upstreamKey()); tok != "" {
		candidate = p.playbackPath(r.Context(), up, item, msID, tok)
	}
	if candidate == "" {
		candidate = p.strmSource(item, msID)
	}
	if candidate == "" {
		return false
	}

	// 秒传：这个 Emby 用户若配了映射账号，先把文件秒过去再用目标账号播放。
	// 成功会就地改写 candidate（换 pickcode 与 uid），失败则原样继续。
	instUser := p.embyUserFor(r, item)
	log.Printf("反代候选：item=%s user=%q → %s", item, instUser, proxyShortURL(candidate))
	if rewritten, ok := p.app.ApplyInstantForEmby(r.Context(), instUser, candidate); ok {
		candidate = rewritten
	}

	final := p.resolve(r.Context(), candidate, r, p.userOf(r, item))
	p.putURL(key, final)
	p.answerRedirect(w, r, item, final, "PlaybackInfo")
	return true
}

func (p *EmbyProxy) answerRedirect(w http.ResponseWriter, r *http.Request, item, final, via string) {
	p.redirects.Add(1)
	log.Printf("反代 302：item=%s（%s）→ %s", item, via, proxyShortURL(final))
	writeCORS(w)
	w.Header().Set("Cache-Control", "no-store")
	http.Redirect(w, r, final, http.StatusFound)
}

func writeCORS(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Access-Control-Allow-Origin", "*")
	h.Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
	h.Set("Access-Control-Allow-Headers", "*")
	h.Set("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges, Content-Type")
}

// playbackPath 问 Emby 要 MediaSources[].Path，挑出 http 地址。
func (p *EmbyProxy) playbackPath(ctx context.Context, up, item, msID, token string) string {
	u := strings.TrimRight(up, "/") + "/Items/" + url.PathEscape(item) + "/PlaybackInfo"
	if token != "" {
		u += "?X-Emby-Token=" + url.QueryEscape(token)
	}
	ctx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, nil)
	if err != nil {
		return ""
	}
	req.Header.Set("X-Emby-Token", token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.follow.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	var data struct {
		MediaSources []struct {
			ID   string `json:"Id"`
			Path string `json:"Path"`
		} `json:"MediaSources"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&data); err != nil {
		return ""
	}
	for _, s := range data.MediaSources {
		if msID != "" && s.ID != msID {
			continue
		}
		if isHTTPURL(s.Path) {
			return s.Path
		}
	}
	return ""
}

// resolve 用 HEAD 跟随重定向，拿到最终直链；失败就退回原始地址。
func (p *EmbyProxy) resolve(ctx context.Context, raw string, r *http.Request, uid string) string {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, raw, nil)
	if err != nil {
		return raw
	}
	copyPlayHeaders(req.Header, r.Header)
	if uid != "" {
		req.Header.Set("X-Emby-UserId", uid)
	}
	resp, err := p.follow.Do(req)
	if err != nil {
		log.Printf("反代解析直链失败（用原地址）：%v", err)
		return raw
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode >= 400 {
		return raw
	}
	if resp.Request != nil && resp.Request.URL != nil {
		return resp.Request.URL.String()
	}
	return raw
}

func copyPlayHeaders(dst, src http.Header) {
	for _, k := range []string{
		"User-Agent", "X-Emby-Token", "X-MediaBrowser-Token", "Authorization",
		"Cookie", "X-Emby-Device-Id", "X-Emby-Client", "X-Emby-Client-Version",
		"X-Emby-Device-Name", "Accept-Language",
	} {
		if v := src.Get(k); v != "" {
			dst.Set(k, v)
		}
	}
}

// ---------------------------------------------------------------- 转发

func (p *EmbyProxy) serve(w http.ResponseWriter, r *http.Request, up string, mod func(*http.Response) error) {
	target, err := url.Parse(up)
	if err != nil {
		http.Error(w, "Emby 服务器地址不合法："+err.Error(), http.StatusBadGateway)
		return
	}
	rp := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			pr.Out.Host = target.Host
			pr.SetXForwarded()
			// 让上游回未压缩正文，改写响应时不用管 br/gzip
			pr.Out.Header.Del("Accept-Encoding")
		},
		Transport:      p.transport,
		ModifyResponse: mod,
		FlushInterval:  200 * time.Millisecond,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("Emby 反代失败 %s %s：%v", r.Method, r.URL.Path, err)
			http.Error(w, "连不上 Emby："+err.Error(), http.StatusBadGateway)
		},
	}
	rp.ServeHTTP(w, r)
}

// rewriteStatic 处理 HTML 壳注入与播放器 js 的 crossOrigin 修补。
func (p *EmbyProxy) rewriteStatic(resp *http.Response) error {
	lower := strings.ToLower(resp.Request.URL.Path)
	switch {
	case strings.HasSuffix(lower, "/htmlvideoplayer/basehtmlplayer.js"):
		return patchJS(resp, baseCrossOriginRe, `"anonymous"`, "basehtmlplayer.js")
	case strings.HasSuffix(lower, "/htmlvideoplayer/plugin.js"):
		return patchJS(resp, pluginCrossOriginRe, "", "plugin.js")
	}
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	if !strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "text/html") {
		return nil
	}
	if !mayBeHTMLShell(resp.Request.URL.Path) {
		return nil
	}
	body, err := readBody(resp)
	if err != nil {
		return nil
	}
	if bytes.Contains(body, []byte(crossOriginMarker)) {
		restoreBody(resp, body)
		return nil
	}
	html := string(body)
	var out string
	if i := strings.Index(strings.ToLower(html), "</head>"); i >= 0 {
		out = html[:i] + crossOriginScript + html[i:]
	} else if i := strings.Index(strings.ToLower(html), "<head"); i >= 0 {
		if j := strings.Index(html[i:], ">"); j >= 0 {
			at := i + j + 1
			out = html[:at] + crossOriginScript + html[at:]
		}
	}
	if out == "" {
		restoreBody(resp, body)
		return nil
	}
	replaceBody(resp, []byte(out))
	resp.Header.Set("Cache-Control", "no-cache, no-store, must-revalidate")
	log.Printf("反代：已注入 crossOrigin 拦截脚本 %s", resp.Request.URL.Path)
	return nil
}

// mayBeHTMLShell 只有页面壳才需要整包读出来注入，别把视频、js、图片也读进内存。
func mayBeHTMLShell(path string) bool {
	pl := strings.ToLower(path)
	if pl == "" || pl == "/" || pl == "/web" || pl == "/emby" || pl == "/emby/web" {
		return true
	}
	if strings.HasSuffix(pl, "/") {
		return true
	}
	if strings.HasSuffix(pl, ".html") || strings.HasSuffix(pl, ".htm") {
		return true
	}
	for _, pre := range []string{"/items/", "/videos/", "/audio/", "/sync/"} {
		if strings.HasPrefix(pl, pre) {
			return false
		}
	}
	last := pl[strings.LastIndex(pl, "/")+1:]
	return !strings.Contains(last, ".")
}

func patchJS(resp *http.Response, re *regexp.Regexp, fallback, label string) error {
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	body, err := readBody(resp)
	if err != nil {
		return nil
	}
	src := string(body)
	out := re.ReplaceAllString(src, "null")
	if out == src && fallback != "" {
		out = strings.ReplaceAll(src, fallback, "null")
		out = strings.ReplaceAll(out, "'anonymous'", "null")
	}
	if out == src {
		restoreBody(resp, body)
		return nil
	}
	replaceBody(resp, []byte(out))
	resp.Header.Set("Content-Type", "application/javascript; charset=utf-8")
	resp.Header.Set("Cache-Control", "no-cache, no-store, must-revalidate")
	log.Printf("反代：已修补 %s（去掉 crossOrigin）", label)
	return nil
}

// rewritePlaybackInfo 命中 strm 源时强制 DirectPlay，并改写 DirectStreamUrl。
func (p *EmbyProxy) rewritePlaybackInfo(resp *http.Response) error {
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	if !strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "json") {
		return nil
	}
	body, err := readBody(resp)
	if err != nil {
		return nil
	}
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		restoreBody(resp, body)
		return nil
	}
	sources, _ := data["MediaSources"].([]any)
	if len(sources) == 0 {
		restoreBody(resp, body)
		return nil
	}

	isStrm := false
	for _, raw := range sources {
		ms, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if path, _ := ms["Path"].(string); isHTTPURL(path) {
			isStrm = true
		}
		// 有些客户端拿到的 Path 是本服务的相对地址（/p115/play/...），
		// 这种也属于 strm 源，不能漏。
		if path, _ := ms["Path"].(string); strings.Contains(path, "/p115/") {
			isStrm = true
		}
		if remote, _ := ms["IsRemote"].(bool); remote {
			if proto, _ := ms["Protocol"].(string); strings.EqualFold(proto, "http") {
				isStrm = true
			}
		}
	}
	if !isStrm {
		restoreBody(resp, body)
		return nil
	}

	item := pathItem(resp.Request.URL.Path)
	// 秒传要看「是哪个 Emby 用户在播」：映射了秒传账号才改写。
	instUser := resp.Request.URL.Query().Get("UserId")
	if instUser == "" {
		instUser = resp.Request.Header.Get("X-Emby-UserId")
	}
	strm := map[string]string{}
	for _, raw := range sources {
		ms, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		ms["SupportsDirectPlay"] = true
		ms["SupportsDirectStream"] = true
		ms["SupportsTranscoding"] = false
		delete(ms, "TranscodingUrl")
		delete(ms, "TranscodingContainer")
		delete(ms, "TranscodingSubProtocol")
		sid, _ := ms["Id"].(string)
		if sid == "" {
			continue
		}
		kind := "videos"
		if t, _ := ms["Type"].(string); strings.EqualFold(t, "audio") {
			kind = "audio"
		}
		ms["DirectStreamUrl"] = fmt.Sprintf("/%s/%s/stream?Static=true&MediaSourceId=%s",
			kind, item, url.QueryEscape(sid))
		if path, _ := ms["Path"].(string); path != "" {
			// 秒传：这个用户映射了秒传账号时，把「源账号的播放地址」换成
			// 秒传路由。只换路径不换 host —— 原来客户端能访问这个地址，
			// 换完也照样能访问。改写在 PlaybackInfo 阶段完成的好处是
			// 播放器一拿到地址就是目标账号的，不用先连源盘再跳一次。
			orig := path
			if inst := p.app.InstantPathFor(orig, instUser); inst != "" {
				log.Printf("反代：PlaybackInfo 命中秒传（item=%s user=%s）→ %s",
					item, instUser, proxyShortURL(inst))
				ms["Path"] = inst
				path = inst
				// 顺手在后台秒传：播放器准备解码器这一两秒正好用掉，
				// 等它来拉流时目标账号里已经有文件了。
				p.app.PrewarmInstant(orig, instUser)
			}
			if isHTTPURL(path) {
				strm[sid] = path
			}
		}
	}
	if len(strm) > 0 {
		p.putStrm(item, strm)
	} else {
		log.Printf("反代：PlaybackInfo 命中 strm，但没读到 http Path（item=%s）", item)
	}
	if instUser != "" {
		p.putUser(userKey(resp.Request, item), instUser)
	}

	out, err := json.Marshal(data)
	if err != nil {
		restoreBody(resp, body)
		return nil
	}
	replaceBody(resp, out)
	resp.Header.Del("ETag")
	resp.Header.Del("Last-Modified")
	log.Printf("反代：PlaybackInfo 已强制 DirectPlay（item=%s，%d 个 http 源）", item, len(strm))
	return nil
}

// rewriteSystemInfo 把客户端要连的端口改成本反代端口。
func (p *EmbyProxy) rewriteSystemInfo(resp *http.Response) error {
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	body, err := readBody(resp)
	if err != nil {
		return nil
	}
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		restoreBody(resp, body)
		return nil
	}
	origin, ok := toInt(data["WebSocketPortNumber"])
	if !ok {
		restoreBody(resp, body)
		return nil
	}
	port := p.EffectivePort()
	// 客户端访问反代时用的端口才是它该连回去的端口；
	// resp.Request 已经被改成上游地址了，只能看 X-Forwarded-Host（= 客户端的 Host）。
	if hp, ok := hostPort(resp.Request.Header.Get("X-Forwarded-Host")); ok {
		port = hp
	}
	if port <= 0 {
		restoreBody(resp, body)
		return nil
	}
	data["WebSocketPortNumber"] = port
	if _, ok := data["HttpServerPortNumber"]; ok {
		data["HttpServerPortNumber"] = port
	}
	for _, k := range []string{"LocalAddresses", "RemoteAddresses"} {
		list, ok := data[k].([]any)
		if !ok {
			continue
		}
		for i, v := range list {
			if s, ok := v.(string); ok {
				list[i] = strings.ReplaceAll(s, fmt.Sprintf(":%d", origin), fmt.Sprintf(":%d", port))
			}
		}
	}
	for _, k := range []string{"LocalAddress", "WanAddress"} {
		if s, ok := data[k].(string); ok {
			data[k] = strings.ReplaceAll(s, fmt.Sprintf(":%d", origin), fmt.Sprintf(":%d", port))
		}
	}
	out, err := json.Marshal(data)
	if err != nil {
		restoreBody(resp, body)
		return nil
	}
	replaceBody(resp, out)
	resp.Header.Del("ETag")
	resp.Header.Del("Last-Modified")
	return nil
}

// ---------------------------------------------------------------- 工具

func isHTTPURL(s string) bool {
	return strings.HasPrefix(s, "http://") || strings.HasPrefix(s, "https://")
}

// proxyShortURL 日志里只留 host+path，别把带签名的直链打出去。
func proxyShortURL(link string) string {
	u, err := url.Parse(link)
	if err != nil {
		return "?"
	}
	return u.Scheme + "://" + u.Host + u.Path
}

func pathItem(path string) string {
	seg := strings.Split(strings.Trim(path, "/"), "/")
	if len(seg) >= 2 && strings.EqualFold(seg[0], "emby") {
		seg = seg[1:]
	}
	if len(seg) >= 2 {
		return seg[1]
	}
	return ""
}

func apiKeyOf(r *http.Request, fallback string) string {
	q := r.URL.Query()
	for _, k := range []string{"X-Emby-Token", "x-emby-token", "api_key", "ApiKey"} {
		if v := q.Get(k); v != "" {
			return v
		}
	}
	for _, k := range []string{"X-Emby-Token", "X-MediaBrowser-Token"} {
		if v := r.Header.Get(k); v != "" {
			return v
		}
	}
	auth := strings.ToLower(r.Header.Get("Authorization"))
	if i := strings.Index(auth, "token="); i >= 0 {
		tok := r.Header.Get("Authorization")[i+6:]
		if j := strings.IndexAny(tok, ",; "); j >= 0 {
			tok = tok[:j]
		}
		if tok = strings.Trim(tok, `"`); tok != "" {
			return tok
		}
	}
	return fallback
}

func userKey(r *http.Request, item string) string {
	return clientIPOf(r) + "|" + r.UserAgent() + "|" + item
}

// embyUserFor 取这次播放对应的 Emby 用户。
//
// 优先用 PlaybackInfo 时缓存的（key 里带 IP+UA，最准）；但播放器有时会换 UA
// （比如换播放内核后 UA 变了），缓存就命不中，这时退回请求头里的 X-Emby-UserId，
// 否则秒传会因为「认不出是谁」被静默跳过。
func (p *EmbyProxy) embyUserFor(r *http.Request, item string) string {
	if uid := p.userOf(r, item); uid != "" {
		return uid
	}
	for _, k := range []string{"X-Emby-UserId", "X-MediaBrowser-UserId"} {
		if uid := strings.TrimSpace(r.Header.Get(k)); uid != "" {
			return uid
		}
	}
	if uid := strings.TrimSpace(r.URL.Query().Get("UserId")); uid != "" {
		return uid
	}
	return ""
}

func clientIPOf(r *http.Request) string {
	if v := r.Header.Get("X-Forwarded-For"); v != "" {
		return strings.TrimSpace(strings.Split(v, ",")[0])
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func headerHash(r *http.Request) string {
	keys := []string{
		"Authorization", "Cookie", "X-Emby-Token", "User-Agent", "X-Emby-Device-Id",
		"X-Emby-Device-Name", "X-Emby-Client", "X-Emby-Client-Version", "X-Device-Id",
	}
	var b strings.Builder
	for _, k := range keys {
		if v := r.Header.Get(k); v != "" {
			b.WriteString(k)
			b.WriteString(":")
			b.WriteString(v)
			b.WriteString("\n")
		}
	}
	return fmt.Sprintf("%x", fnv64(b.String()))
}

func fnv64(s string) uint64 {
	var h uint64 = 1469598103934665603
	for i := 0; i < len(s); i++ {
		h ^= uint64(s[i])
		h *= 1099511628211
	}
	return h
}

func hostPort(host string) (int, bool) {
	_, portStr, err := net.SplitHostPort(host)
	if err != nil {
		return 0, false
	}
	var n int
	for _, ch := range portStr {
		if ch < '0' || ch > '9' {
			return 0, false
		}
		n = n*10 + int(ch-'0')
	}
	if n <= 0 {
		return 0, false
	}
	return n, true
}

func toInt(v any) (int, bool) {
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case json.Number:
		n, err := t.Int64()
		return int(n), err == nil
	}
	return 0, false
}

func readBody(resp *http.Response) ([]byte, error) {
	var rd io.Reader = resp.Body
	switch strings.ToLower(resp.Header.Get("Content-Encoding")) {
	case "gzip":
		zr, err := gzip.NewReader(resp.Body)
		if err != nil {
			return nil, err
		}
		defer zr.Close()
		rd = zr
	case "deflate":
		fr := flate.NewReader(resp.Body)
		defer fr.Close()
		rd = fr
	}
	body, err := io.ReadAll(rd)
	_ = resp.Body.Close()
	return body, err
}

func restoreBody(resp *http.Response, body []byte) {
	resp.Body = io.NopCloser(bytes.NewReader(body))
	resp.ContentLength = int64(len(body))
}

func replaceBody(resp *http.Response, body []byte) {
	resp.Body = io.NopCloser(bytes.NewReader(body))
	resp.ContentLength = int64(len(body))
	resp.Header.Del("Content-Encoding")
	resp.Header.Del("Content-Length")
	resp.Header.Del("ETag")
	resp.Header.Del("Last-Modified")
}

// ---------------------------------------------------------------- 缓存

func (p *EmbyProxy) getURL(key string) (string, bool) {
	p.cacheMu.Lock()
	defer p.cacheMu.Unlock()
	e, ok := p.urls[key]
	if !ok {
		return "", false
	}
	if time.Now().After(e.expire) {
		delete(p.urls, key)
		return "", false
	}
	return e.url, true
}

func (p *EmbyProxy) putURL(key, u string) {
	p.cacheMu.Lock()
	defer p.cacheMu.Unlock()
	if len(p.urls) > 2000 {
		p.urls = map[string]proxyURLEntry{}
	}
	p.urls[key] = proxyURLEntry{url: u, expire: time.Now().Add(proxyURLCacheTTL)}
}

func (p *EmbyProxy) strmSource(item, msID string) string {
	p.cacheMu.Lock()
	defer p.cacheMu.Unlock()
	e, ok := p.strms[item]
	if !ok || time.Now().After(e.expire) {
		delete(p.strms, item)
		return ""
	}
	if msID != "" {
		if v, ok := e.sources[msID]; ok {
			return v
		}
	}
	for _, v := range e.sources {
		return v
	}
	return ""
}

func (p *EmbyProxy) putStrm(item string, sources map[string]string) {
	p.cacheMu.Lock()
	defer p.cacheMu.Unlock()
	if len(p.strms) > 2000 {
		p.strms = map[string]proxyStrmEntry{}
	}
	p.strms[item] = proxyStrmEntry{sources: sources, expire: time.Now().Add(proxyStrmCacheTTL)}
}

func (p *EmbyProxy) userOf(r *http.Request, item string) string {
	p.cacheMu.Lock()
	defer p.cacheMu.Unlock()
	e, ok := p.users[userKey(r, item)]
	if !ok || time.Now().After(e.expire) {
		delete(p.users, userKey(r, item))
		return ""
	}
	return e.uid
}

func (p *EmbyProxy) putUser(key, uid string) {
	p.cacheMu.Lock()
	defer p.cacheMu.Unlock()
	if len(p.users) > 2000 {
		p.users = map[string]proxyUserEntry{}
	}
	p.users[key] = proxyUserEntry{uid: uid, expire: time.Now().Add(proxyUserCacheTTL)}
}
