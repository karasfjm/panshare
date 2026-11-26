// 动态企微可信 IP 插件（独立进程，由主服务反代 /api/plugins/wxip/proxy/）。
//
// 核心流程移植自 JYONG719/WXIPUpdaterr：扫码登录企微管理后台 → 检测公网出口 IP
// → IP 变了自动打开自建应用的「企业可信IP」配置，把新 IP 追加进去。
// 纯 HTTP 服务，浏览器只在做事时才起；登录态、配置、历史都落在本插件的数据目录。
package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"panshare/backend/plugins/wxip/core"
)

//go:embed ui.html
var uiHTML []byte

func main() {
	addr := flag.String("addr", "127.0.0.1:18101", "监听地址")
	dataDir := flag.String("data", "./data", "数据目录")
	// 主服务按统一契约给所有插件传这几个参数；wxip 不写 strm，接收但不使用，
	// 否则 flag.Parse() 会直接退出，插件永远起不来。
	flag.String("strm-root", "", "（兼容主服务参数，wxip 不使用）")
	flag.String("base-url", "", "（兼容主服务参数，wxip 不使用）")
	flag.String("play-prefix", "", "（兼容主服务参数，wxip 不使用）")
	flag.Parse()

	if err := os.MkdirAll(*dataDir, 0o755); err != nil {
		log.Fatalf("创建数据目录失败：%v", err)
	}

	kv := core.NewKV(*dataDir)
	mgr := core.New(*dataDir, kv, nil)
	stop := make(chan struct{})
	go mgr.RunForever(stop)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "ok")
	})
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(uiHTML)
	})
	mux.HandleFunc("GET /api/state", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, mgr.Status())
	})
	mux.HandleFunc("POST /api/settings", func(w http.ResponseWriter, r *http.Request) {
		var patch map[string]any
		_ = decodeJSON(r, &patch)
		if patch == nil {
			patch = map[string]any{}
		}
		cur := mgr.SaveSettings(patch)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "settings": cur, "status": mgr.Status()})
	})
	// 提交验证码：浏览器只在登录 goroutine 里操作，这里只把 code 存进
	// Manager，由登录轮询消费并回填。
	mux.HandleFunc("POST /api/verify", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Code string `json:"code"`
		}
		_ = decodeJSON(r, &body)
		writeJSON(w, http.StatusOK, mgr.RequestVerifyCode(body.Code))
	})
	mux.HandleFunc("POST /api/{action}", func(w http.ResponseWriter, r *http.Request) {
		var result map[string]any
		switch r.PathValue("action") {
		case "login":
			result = mgr.RequestLogin()
		case "update":
			result = mgr.RequestUpdate()
		case "shot":
			result = mgr.RequestShot()
		case "logout":
			result = mgr.Logout()
		case "browser":
			result = mgr.RequestInstallBrowser()
		default:
			writeErr(w, http.StatusNotFound, "没有这个动作")
			return
		}
		writeJSON(w, http.StatusOK, result)
	})

	srv := &http.Server{Addr: *addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-quit
		close(stop)
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()

	log.Printf("企微可信IP 插件已启动 %s（数据 %s）", *addr, filepath.Clean(*dataDir))
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("服务退出：%v", err)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"ok": false, "detail": msg})
}

func decodeJSON(r *http.Request, v any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	return dec.Decode(v)
}

// 让 fmt / strings 在静态检查里不被判定为多余（页面/日志未来会用到）
var _ = fmt.Sprintf
var _ = strings.TrimSpace
