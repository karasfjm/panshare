package server

import (
	"io"
	"log"
	"net/http"
	"net/url"
	"path"
	"strings"
	"sync"
	"time"

	"panshare/internal/p115"
)

// 播放出口有两种：
//
//   - **302 直跳**：链接只校验 UA（f≠3），播放器拿到 302 后直连 115 CDN，
//     视频流量不经过本服务，最省带宽。
//   - **本机中转**：链接带 f=3 时，CDN 还要求带上「取链接那次响应」下发的
//     cookie。302 没法把别的域名的 cookie 交给播放器，只能由本服务代拉。
//
// 视频文件基本都会走到第二种（115 对视频/大文件一律给 f=3）。
var mediaClient = &http.Client{
	Transport: &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: 40 * time.Second,
		DisableCompression:    true, // 视频不能再压
	},
	// 不设总超时：一集几十个GB时整条流的耗时是不可控的，由请求 context 控制
}

// serveLink 按链接形态决定 302 还是中转。
func serveLink(w http.ResponseWriter, r *http.Request, link string, header map[string]string) {
	if link == "" {
		http.Error(w, "没有可用的直链", http.StatusBadGateway)
		return
	}
	if needRelay(link, header, r.UserAgent()) {
		logPlayOnce(link, "播放：中转（115 直链要 cookie，f=3）← %s", shortUA(r))
		relayMedia(w, r, link, header)
		return
	}
	logPlayOnce(link, "播放：302 直连 115 ← %s", shortUA(r))
	redirect(w, r, link)
}

// playLogAt 给播放日志限流：拖动进度会连着来几十个 Range 请求，只记第一次。
var (
	playLogMu sync.Mutex
	playLogAt = map[string]time.Time{}
)

func logPlayOnce(link, format string, args ...any) {
	key := shortURL(link)
	playLogMu.Lock()
	if last, ok := playLogAt[key]; ok && time.Since(last) < 30*time.Second {
		playLogMu.Unlock()
		return
	}
	playLogAt[key] = time.Now()
	if len(playLogAt) > 512 {
		playLogAt = map[string]time.Time{key: time.Now()}
	}
	playLogMu.Unlock()
	log.Printf(format, args...)
}

// shortURL 去掉签名参数，日志里不留可用的下载凭据。
func shortURL(link string) string {
	u, err := url.Parse(link)
	if err != nil {
		return "?"
	}
	return u.Scheme + "://" + u.Host + u.Path
}

func shortUA(r *http.Request) string {
	ua := r.UserAgent()
	if ua == "" {
		return "无 UA"
	}
	if len(ua) > 60 {
		ua = ua[:60] + "…"
	}
	return ua
}

func needRelay(link string, header map[string]string, clientUA string) bool {
	// 只认 f 参数：每个响应都会下发 acw_tc（WAF cookie），
	// 不能因为「有 cookie」就中转，否则所有播放都走本机流量。
	if u, err := url.Parse(link); err == nil {
		switch u.Query().Get("f") {
		case "3":
			return true
		case "1":
			// f=1 的直链 CDN 只校验 UA，而取链接用的 UA 就是播放器自己的，
			// 302 之后播放器带同一个 UA，能直连。
			// 但客户端压根不发 UA 时（个别机顶盒/播放器），我们取链接用的是兜底 UA，
			// 302 过去必然对不上 → 403，这种情况老实中转。
			return strings.TrimSpace(clientUA) == ""
		default:
			return false
		}
	}
	return header["Cookie"] != "" && (strings.Contains(link, "&f=3") || strings.Contains(link, "?f=3"))
}

// relayMedia 代拉 115 CDN 的字节流，保留 Range 语义，支持拖动进度。
func relayMedia(w http.ResponseWriter, r *http.Request, link string, header map[string]string) {
	req, err := http.NewRequestWithContext(r.Context(), r.Method, link, nil)
	if err != nil {
		http.Error(w, "构造请求失败", http.StatusBadGateway)
		return
	}
	for _, k := range []string{"Range", "If-Range", "Accept", "Accept-Language"} {
		if v := r.Header.Get(k); v != "" {
			req.Header.Set(k, v)
		}
	}
	ua := header["User-Agent"]
	if ua == "" {
		ua = r.Header.Get("User-Agent")
	}
	if ua == "" {
		ua = p115.UserAgent
	}
	req.Header.Set("User-Agent", ua)
	if ck := header["Cookie"]; ck != "" {
		req.Header.Set("Cookie", ck)
	}

	resp, err := mediaClient.Do(req)
	if err != nil {
		log.Printf("中转拉流失败: %v", err)
		http.Error(w, "拉流失败："+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for _, k := range []string{
		"Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
		"Last-Modified", "ETag",
	} {
		if v := resp.Header.Get(k); v != "" {
			w.Header().Set(k, v)
		}
	}
	// 115 的 CDN 对视频一律回 application/octet-stream，
	// Infuse / Kodi 这类播放器会按 Content-Type 判断能不能播，这里按扩展名补上。
	if ct := w.Header().Get("Content-Type"); ct == "" || ct == "application/octet-stream" {
		if m := mediaType(link); m != "" {
			w.Header().Set("Content-Type", m)
		}
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges, Content-Type")
	w.WriteHeader(resp.StatusCode)
	if r.Method == http.MethodHead {
		return
	}
	buf := make([]byte, 64<<10)
	_, _ = io.CopyBuffer(w, resp.Body, buf)
}

// mediaType 按链接里的文件扩展名给出 Content-Type。
func mediaType(link string) string {
	u, err := url.Parse(link)
	if err != nil {
		return ""
	}
	ext := strings.ToLower(path.Ext(u.Path))
	switch ext {
	case ".mkv":
		return "video/x-matroska"
	case ".mp4", ".m4v":
		return "video/mp4"
	case ".ts", ".m2ts", ".mts":
		return "video/mp2t"
	case ".avi":
		return "video/x-msvideo"
	case ".wmv":
		return "video/x-ms-wmv"
	case ".mov":
		return "video/quicktime"
	case ".flv":
		return "video/x-flv"
	case ".rmvb", ".rm":
		return "video/vnd.rn-realvideo"
	case ".webm":
		return "video/webm"
	case ".mpg", ".mpeg":
		return "video/mpeg"
	case ".srt":
		return "application/x-subrip"
	case ".ass", ".ssa":
		return "text/x-ssa"
	case ".nfo":
		return "text/plain; charset=utf-8"
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".png":
		return "image/png"
	}
	return ""
}
