// PanShare 后端入口：Go + SQLite，单二进制。
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"panshare/internal/config"
	"panshare/internal/db"
	"panshare/internal/server"
	"panshare/internal/service"
	"panshare/internal/strm"
)

func main() {
	webDir := flag.String("web", "./web", "前端静态文件目录")
	flag.Parse()

	log.SetFlags(log.LstdFlags)
	cfg := config.Load()

	database, err := db.Open(cfg.DBPath())
	if err != nil {
		log.Fatalf("打开数据库失败: %v", err)
	}
	defer database.Close()

	store, err := strm.New(cfg.StrmRoot)
	if err != nil {
		log.Fatalf("初始化映射根失败: %v", err)
	}
	log.Printf("映射根: %s", store.Root)

	app := service.NewApp(cfg, database, store)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	app.Start(ctx)

	if v := os.Getenv("PS_VERSION"); v != "" {
		server.Version = v
	}
	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           server.New(app, *webDir).Handler(),
		ReadHeaderTimeout: 20 * time.Second,
	}
	go func() {
		log.Printf("PanShare 已启动: http://0.0.0.0%s", cfg.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务退出: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	log.Printf("正在退出…")
	cancel()
	shutdownCtx, stop := context.WithTimeout(context.Background(), 10*time.Second)
	defer stop()
	_ = srv.Shutdown(shutdownCtx)
}
