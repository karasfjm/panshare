// Package config 负责从环境变量读取运行期配置。
//
// 约定：所有配置项都可以用环境变量给出，容器里通过 docker-compose 注入；
// 需要用户随时改的开关（同播复制、联动删除、定时巡检……）放在数据库的
// settings 表里，由前端页面修改，不走这里。
package config

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	// HTTP 监听地址，例如 :9527
	Addr string
	// 数据目录：sqlite、插件状态、日志都在这里
	DataDir string
	// strm 映射根。strm 文件只允许写在这个目录下面
	StrmRoot string
	// 写进 strm 的请求头，必须是播放器（Emby/电视/手机）能访问到的地址
	PublicBaseURL string
	// 控制台登录
	Username    string
	Password    string
	SessionKey  string // 签名 cookie 用，空则用密码派生
	DisableAuth bool
	// 共享代理：Telegram 与 TMDB 登录背景共用同一个出口。
	// 留空时回落到环境变量 HTTPS_PROXY（由 http.ProxyFromEnvironment 处理）。
	Proxy string
	// TMDB API Key（v3 的 32 位串或 v4 的 Bearer token）。
	// 登录页随机背景从 TMDB 热门影视取 backdrop，没有 key 时前端回落到渐变。
	TMDBAPIKey string
	// 插件目录（内置插件从这里读取，安装后复制到 DataDir/plugins）
	PluginDir string
	// 时间区
	TZ string
	// 115 全局请求节流（每秒请求数），太高会被 WAF 拦成 405
	APITimeoutSec int
	// Emby 反代监听端口（独立端口，整站反代到第一台启用的 Emby）。
	// 0 表示关闭：容器里不映射这个端口就不会对外暴露。
	EmbyProxyPort int
}

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

// firstEnv 返回第一个非空环境变量，便于同时兼容带前缀和裸变量名的写法。
func firstEnv(keys ...string) string {
	for _, k := range keys {
		if v := strings.TrimSpace(os.Getenv(k)); v != "" {
			return v
		}
	}
	return ""
}

func Load() *Config {
	dataDir := env("PS_DATA_DIR", "./data")
	if abs, err := filepath.Abs(dataDir); err == nil {
		dataDir = abs
	}
	c := &Config{
		Addr:          env("PS_ADDR", ":9527"),
		DataDir:       dataDir,
		StrmRoot:      env("PS_STRM_ROOT", "/strm"),
		PublicBaseURL: strings.TrimRight(env("PS_PUBLIC_BASE_URL", "http://127.0.0.1:9527"), "/"),
		Username:      env("PS_USERNAME", "admin"),
		Password:      env("PS_PASSWORD", "password"),
		SessionKey:    env("PS_SESSION_KEY", ""),
		DisableAuth:   env("PS_DISABLE_AUTH", "") == "1",
		Proxy:         firstEnv("PS_PROXY", "PROXY"),
		TMDBAPIKey:    firstEnv("PS_TMDB_API_KEY", "TMDB_API_KEY"),
		PluginDir:     env("PS_PLUGIN_DIR", "./plugins"),
		TZ:            env("PS_TZ", "Asia/Shanghai"),
		APITimeoutSec: atoi(env("PS_API_TIMEOUT", "30"), 30),
		EmbyProxyPort: atoi(env("PS_EMBY_PROXY_PORT", "7259"), 7259),
	}
	c.PluginDir = resolvePluginDir(c.PluginDir)
	return c
}

// resolvePluginDir 兜一层插件目录：配置的目录不存在时，退到可执行文件旁边的
// plugins（本地直接跑二进制时不用再设环境变量）。
func resolvePluginDir(configured string) string {
	if configured != "" {
		if _, err := os.Stat(configured); err == nil {
			return configured
		}
	}
	exe, err := os.Executable()
	if err != nil {
		return configured
	}
	sibling := filepath.Join(filepath.Dir(exe), "plugins")
	if _, err := os.Stat(sibling); err == nil {
		return sibling
	}
	return configured
}

func (c *Config) DBPath() string       { return filepath.Join(c.DataDir, "panshare.db") }
func (c *Config) InstalledPlugins() string { return filepath.Join(c.DataDir, "plugins") }

func atoi(s string, def int) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return n
}
