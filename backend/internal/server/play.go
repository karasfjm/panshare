package server

import (
	"context"
	"errors"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// 播放路由：Emby / 播放器请求到这里，换取新鲜直链后 302。
// 视频流量直连 115 CDN，本服务只做「解析 + 跳转」，不中转数据。

func (s *Server) play(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		playOptions(w)
		return
	}
	pickcode := r.PathValue("pickcode")
	uid := r.PathValue("uid")
	if pickcode == "" {
		http.Error(w, "缺少 pickcode", http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()
	link, header, err := s.app.ResolvePlay(ctx, pickcode, uid, clientIP(r), r.Header.Get("User-Agent"))
	if err != nil {
		http.Error(w, "解析直链失败："+err.Error(), http.StatusBadGateway)
		return
	}
	serveLink(w, r, link, header)
}

func (s *Server) instantPlay(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		playOptions(w)
		return
	}
	// 路径里是 /p115/instant/<sha1>[.<ext>]，扩展名只是让播放器认容器格式
	sha1 := r.PathValue("sha1")
	if i := strings.Index(sha1, "."); i > 0 {
		sha1 = sha1[:i]
	}
	sha1 = strings.ToUpper(strings.TrimSpace(sha1))
	if len(sha1) != 40 {
		http.Error(w, "sha1 格式不对", http.StatusBadRequest)
		return
	}
	q := r.URL.Query()
	accountID := q.Get("account")
	if accountID == "" {
		http.Error(w, "缺少 account 参数", http.StatusBadRequest)
		return
	}
	size, _ := strconv.ParseInt(q.Get("size"), 10, 64)
	// 秒传要传校验块 + 等 115 建文件，比普通播放慢，超时给宽一点
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	pick, err := s.app.InstantDeduped(ctx, accountID, sha1, q.Get("name"), size, q.Get("source"), "")
	if err != nil {
		http.Error(w, "秒传失败："+err.Error(), http.StatusBadGateway)
		return
	}
	link, header, err := s.app.ResolvePlayInAccount(ctx, accountID, pick, clientIP(r), r.Header.Get("User-Agent"))
	if err != nil {
		http.Error(w, "解析直链失败："+err.Error(), http.StatusBadGateway)
		return
	}
	serveLink(w, r, link, header)
}

func (s *Server) sharePlay(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		playOptions(w)
		return
	}
	code := r.PathValue("code")
	fileID := r.PathValue("file")
	if code == "" || fileID == "" {
		http.Error(w, "缺少分享码或文件 id", http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()
	link, err := s.app.ResolveSharePlay(ctx, code, fileID, r.URL.Query().Get("rc"))
	if err != nil {
		http.Error(w, "解析分享直链失败："+err.Error(), http.StatusBadGateway)
		return
	}
	serveLink(w, r, link, nil)
}

func (s *Server) ed2kPlay(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		playOptions(w)
		return
	}
	hash := r.PathValue("hash")
	if hash == "" {
		http.Error(w, "缺少 ed2k hash", http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	link, err := s.app.ResolveEd2kPlay(ctx, hash, r.Header.Get("User-Agent"))
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return
		}
		http.Error(w, "ed2k 还没准备好："+err.Error(), http.StatusServiceUnavailable)
		return
	}
	serveLink(w, r, link, nil)
}

func redirect(w http.ResponseWriter, r *http.Request, link string) {
	if link == "" {
		http.Error(w, "没有可用的直链", http.StatusBadGateway)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	http.Redirect(w, r, link, http.StatusFound)
}

// playOptions 回 CORS 预检：Emby Web 从反代端口跳到播放端口是跨域的，
// 浏览器对带 Range 的媒体请求会先发 OPTIONS，不答就直接播不了。
func playOptions(w http.ResponseWriter) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "*")
	w.Header().Set("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")
	w.Header().Set("Access-Control-Max-Age", "86400")
	w.WriteHeader(http.StatusNoContent)
}

func clientIP(r *http.Request) string {
	if v := r.Header.Get("X-Forwarded-For"); v != "" {
		return strings.TrimSpace(strings.Split(v, ",")[0])
	}
	if v := r.Header.Get("X-Real-IP"); v != "" {
		return v
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
