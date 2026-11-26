package server

import (
	"net/http"
	"strconv"
)

// loginBackground 给登录页提供一张 TMDB 热门影视随机背景。
//
// 故意公开（不需要会话），否则未登录时看不到背景。API Key 只在后端使用，
// 错误时返回 204，由前端回落到渐变背景，不暴露 Key 或代理地址。
func (s *Server) loginBackground(w http.ResponseWriter, r *http.Request) {
	data, ctype, err := s.app.TMDB.Background()
	if err != nil || len(data) == 0 {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	_, _ = w.Write(data)
}
