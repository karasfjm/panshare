// Package server 是对外的 HTTP 层：控制台 API、播放 302、插件反代。
package server

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"panshare/internal/service"
)

const cookieName = "ps_session"

type Server struct {
	app    *service.App
	webDir string
	key    []byte
	noAuth bool
	user   string
	pass   string
}

func New(app *service.App, webDir string) *Server {
	key := []byte(app.Cfg.SessionKey)
	if len(key) == 0 {
		sum := sha256.Sum256([]byte("panshare|" + app.Cfg.Password))
		key = sum[:]
	}
	return &Server{
		app: app, webDir: webDir, key: key,
		noAuth: app.Cfg.DisableAuth || app.Cfg.Password == "",
		user:   app.Cfg.Username, pass: app.Cfg.Password,
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()

	// 登录（background 是公开接口：未登录时也要能看到背景）
	mux.HandleFunc("POST /api/login", s.login)
	mux.HandleFunc("POST /api/logout", s.logout)
	mux.HandleFunc("GET /api/me", s.me)
	mux.HandleFunc("GET /api/login/background", s.loginBackground)

	// 容器健康检查专用：不打日志、不查库，避免每 30 秒刷一行 /api/me
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("ok"))
	})

	// 账号
	mux.Handle("GET /api/accounts", s.auth(s.accounts))
	mux.Handle("POST /api/accounts/cookie", s.auth(s.addCookie))
	mux.Handle("POST /api/accounts/qr", s.auth(s.startQR))
	mux.Handle("GET /api/accounts/qr/{uid}", s.auth(s.pollQR))
	mux.Handle("PATCH /api/accounts/{id}", s.auth(s.updateAccount))
	mux.Handle("DELETE /api/accounts/{id}", s.auth(s.deleteAccount))
	mux.Handle("POST /api/accounts/{id}/refresh", s.auth(s.refreshAccount))
	mux.Handle("POST /api/accounts/{id}/default", s.auth(s.setDefaultAccount))

	// 目录
	mux.Handle("GET /api/dirs", s.auth(s.dirs))
	mux.Handle("GET /api/locals", s.auth(s.localDirs))

	// 115 STRM
	mux.Handle("GET /api/strm/configs", s.auth(s.listConfigs))
	mux.Handle("POST /api/strm/configs", s.auth(s.createConfig))
	mux.Handle("PATCH /api/strm/configs/{id}", s.auth(s.updateConfig))
	mux.Handle("DELETE /api/strm/configs/{id}", s.auth(s.deleteConfig))
	mux.Handle("POST /api/strm/configs/{id}/run", s.auth(s.runConfig))
	mux.Handle("GET /api/strm/configs/{id}/files", s.auth(s.configFiles))
	mux.Handle("GET /api/files", s.auth(s.searchFiles))
	mux.Handle("GET /api/strm/cleanups", s.auth(s.listCleanups))
	mux.Handle("POST /api/strm/cleanups/{id}/approve", s.auth(s.approveCleanup))
	mux.Handle("DELETE /api/strm/cleanups/{id}", s.auth(s.cancelCleanup))

	// 分享转存
	mux.Handle("GET /api/shares", s.auth(s.listShares))
	mux.Handle("POST /api/shares", s.auth(s.createShare))
	mux.Handle("DELETE /api/shares/{id}", s.auth(s.deleteShare))

	// ed2k
	mux.Handle("GET /api/tasks/defaults", s.auth(s.taskDefaults))
	mux.Handle("POST /api/tasks/defaults", s.auth(s.saveTaskDefaults))
	mux.Handle("GET /api/ed2k", s.auth(s.listEd2k))
	mux.Handle("POST /api/ed2k", s.auth(s.addEd2k))
	mux.Handle("DELETE /api/ed2k/{id}", s.auth(s.deleteEd2k))
	mux.Handle("GET /api/ed2k/export", s.auth(s.exportEd2k))
	mux.Handle("POST /api/ed2k/import", s.auth(s.importEd2k))

	// 秒传
	mux.Handle("GET /api/instant", s.auth(s.listInstant))
	mux.Handle("POST /api/instant/targets", s.auth(s.saveInstantTarget))
	mux.Handle("DELETE /api/instant/targets/{id}", s.auth(s.deleteInstantTarget))
	mux.Handle("POST /api/instant/mappings", s.auth(s.saveInstantMapping))
	mux.Handle("DELETE /api/instant/mappings/{id}", s.auth(s.deleteInstantMapping))

	// Emby
	mux.Handle("GET /api/emby/servers", s.auth(s.listEmby))
	mux.Handle("POST /api/emby/servers", s.auth(s.saveEmby))
	mux.Handle("DELETE /api/emby/servers/{id}", s.auth(s.deleteEmby))
	mux.Handle("POST /api/emby/servers/test", s.auth(s.testEmby))
	mux.Handle("GET /api/emby/users", s.auth(s.embyUsers))
	mux.Handle("GET /api/emby/proxy", s.auth(s.embyProxyInfo))
	mux.Handle("POST /api/emby/proxy", s.auth(s.saveEmbyProxy))

	// 设置 / 事件 / 联动 / 插件
	mux.Handle("GET /api/settings", s.auth(s.getSettings))
	mux.Handle("POST /api/settings", s.auth(s.saveSettings))
	mux.Handle("GET /api/events", s.auth(s.listEvents))
	mux.Handle("DELETE /api/events", s.auth(s.clearEvents))
	mux.Handle("GET /api/stats", s.auth(s.stats))
	mux.Handle("GET /api/link/deletions", s.auth(s.listDeletions))
	mux.Handle("POST /api/link/scan", s.auth(s.linkScan))
	mux.Handle("POST /api/link/deletions/{id}/confirm", s.auth(s.confirmDeletion))
	mux.Handle("DELETE /api/link/deletions/{id}", s.auth(s.cancelDeletion))
	mux.Handle("POST /api/tg/test", s.auth(s.testTG))
	mux.Handle("GET /api/tg/status", s.auth(s.tgStatus))
	mux.Handle("POST /api/wechat/test", s.auth(s.testWeChat))
	mux.Handle("GET /api/wechat/status", s.auth(s.wechatStatus))
	// 把指令同步成企业微信应用的自定义菜单（点一下就当指令执行）
	mux.Handle("POST /api/wechat/app/menu", s.auth(s.wxAppMenuPush))
	// 微信服务号回调：请求来自公众号后台，没有登录态，必须公开。
	// 安全性靠 URL 上的 signature / msg_signature 校验（Token + EncodingAESKey）。
	mux.HandleFunc("GET /api/wechat/mp/callback", s.wxMPCallback)
	mux.HandleFunc("POST /api/wechat/mp/callback", s.wxMPCallback)
	// 企业微信「接收消息」回调：企业微信要求配「企业可信IP」前先设好它
	mux.HandleFunc("GET /api/wechat/app/callback", s.wxAppCallback)
	mux.HandleFunc("POST /api/wechat/app/callback", s.wxAppCallback)
	mux.Handle("GET /api/plugins", s.auth(s.listPlugins))
	mux.Handle("POST /api/plugins/{id}/install", s.auth(s.installPlugin))
	mux.Handle("POST /api/plugins/{id}/start", s.auth(s.startPlugin))
	mux.Handle("POST /api/plugins/{id}/stop", s.auth(s.stopPlugin))
	mux.Handle("DELETE /api/plugins/{id}", s.auth(s.uninstallPlugin))
	mux.HandleFunc("/api/plugins/{id}/proxy/", s.pluginProxy)

	// 播放（播放器直接访问，不能要求登录）
	mux.HandleFunc("/p115/play/{pickcode}/{name}/{uid}", s.play)
	mux.HandleFunc("/p115/play/{pickcode}/{uid}", s.play)
	// 秒传播放：/p115/instant/<sha1>[.<ext>]?account=..&size=..&name=..&source=..
	mux.HandleFunc("/p115/instant/{sha1}", s.instantPlay)
	mux.HandleFunc("/p115/share/{code}/{file}", s.sharePlay)
	mux.HandleFunc("/p115/share/{code}/{file}/{name}", s.sharePlay)
	mux.HandleFunc("/p115/ed2k/{hash}", s.ed2kPlay)
	mux.HandleFunc("/p115/ed2k/{hash}/{name}", s.ed2kPlay)

	// Emby 反代跑在独立端口上，控制台这个路径只是给个明确答复
	mux.HandleFunc("/emby/", func(w http.ResponseWriter, r *http.Request) {
		port := s.app.Cfg.EmbyProxyPort
		if s.app.Proxy != nil {
			port = s.app.Proxy.EffectivePort()
		}
		if port > 0 {
			http.Error(w, fmt.Sprintf("Emby 反代在 %d 端口，请把客户端地址改成 http://<本机IP>:%d", port, port), http.StatusNotFound)
			return
		}
		http.Error(w, "Emby 反代当前已关闭（页面上把「反代」开关打开即可）", http.StatusNotFound)
	})

	// 控制台静态文件（SPA）
	mux.Handle("/", s.spa())

	return s.logged(mux)
}

// ---------------------------------------------------------------- 登录态

func (s *Server) sign(value string) string {
	mac := hmac.New(sha256.New, s.key)
	mac.Write([]byte(value))
	return hex.EncodeToString(mac.Sum(nil))
}

func (s *Server) issueToken() string {
	exp := time.Now().Add(7 * 24 * time.Hour).Unix()
	payload := fmt.Sprintf("%s|%d", s.user, exp)
	return base64.RawURLEncoding.EncodeToString([]byte(payload)) + "." + s.sign(payload)
}

func (s *Server) valid(r *http.Request) bool {
	if s.noAuth {
		return true
	}
	c, err := r.Cookie(cookieName)
	if err != nil {
		return false
	}
	parts := strings.SplitN(c.Value, ".", 2)
	if len(parts) != 2 {
		return false
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return false
	}
	payload := string(raw)
	if !hmac.Equal([]byte(parts[1]), []byte(s.sign(payload))) {
		return false
	}
	fields := strings.SplitN(payload, "|", 2)
	if len(fields) != 2 {
		return false
	}
	exp, _ := strconv.ParseInt(fields[1], 10, 64)
	return time.Now().Unix() < exp
}

func (s *Server) auth(next http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !s.valid(r) {
			writeErr(w, http.StatusUnauthorized, errors.New("未登录"))
			return
		}
		next(w, r)
	})
}

func (s *Server) logged(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		// /api/me 是前端每次进页面都会调的探活接口，记下来只会把日志刷满
		if strings.HasPrefix(r.URL.Path, "/api/") && r.URL.Path != "/api/me" {
			log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
		}
	})
}

func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if s.noAuth || (body.Username == s.user && body.Password == s.pass) {
		http.SetCookie(w, &http.Cookie{
			Name: cookieName, Value: s.issueToken(), Path: "/",
			HttpOnly: true, SameSite: http.SameSiteLaxMode, MaxAge: 7 * 24 * 3600,
		})
		writeJSON(w, map[string]any{"ok": true, "username": s.user})
		return
	}
	writeErr(w, http.StatusUnauthorized, errors.New("账号或密码不对"))
}

func (s *Server) logout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: "", Path: "/", MaxAge: -1})
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) me(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{
		"logged_in": s.valid(r),
		"username":  s.user,
		"site_name": s.app.Settings().SiteName,
		"no_auth":   s.noAuth,
		"version":   Version,
	})
}

// Version 是构建时注入的版本号。
var Version = "dev"

// ---------------------------------------------------------------- 静态文件

func (s *Server) spa() http.Handler {
	index := filepath.Join(s.webDir, "index.html")
	fs := http.FileServer(http.Dir(s.webDir))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api" || strings.HasPrefix(r.URL.Path, "/api/") {
			http.NotFound(w, r)
			return
		}
		path := filepath.Join(s.webDir, filepath.Clean("/"+r.URL.Path))
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			fs.ServeHTTP(w, r)
			return
		}
		if _, err := os.Stat(index); err != nil {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprint(w, `<meta charset="utf-8"><body style="font-family:system-ui;padding:40px">
<h2>前端还没有构建</h2>
<p>请执行 <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>，
或直接用 Docker 镜像启动（镜像里已经包含构建产物）。</p>
<p>API 在 <code>/api/status</code>。</p></body>`)
			return
		}
		http.ServeFile(w, r, index)
	})
}

// ---------------------------------------------------------------- JSON 辅助

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": err.Error()})
}
