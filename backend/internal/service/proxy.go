package service

import (
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// sharedProxy 返回 Telegram 与 TMDB 共用的代理串。
//
// 优先级：页面「通知」里填的 tg_proxy（历史字段名，现在语义是共享代理）
// → compose 的 PS_PROXY → 环境变量 HTTPS_PROXY（交给 http.ProxyFromEnvironment）。
// 这样登录页还没登录时取 TMDB 背景，也能复用用户给 TG 配的同一个出口。
func sharedProxy(tgProxy string, cfgProxy string) string {
	if p := strings.TrimSpace(tgProxy); p != "" {
		return p
	}
	return strings.TrimSpace(cfgProxy)
}

// SharedProxy 给 server 层用的便捷入口。
func (a *App) SharedProxy() string {
	return sharedProxy(a.Settings().TGProxy, a.Cfg.Proxy)
}

// newProxyClient 按代理串构造 HTTP client。空串表示走环境变量。
func newProxyClient(proxyStr string, timeout time.Duration) (*http.Client, error) {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if proxyStr == "" {
		tr.Proxy = http.ProxyFromEnvironment
		return &http.Client{Timeout: timeout, Transport: tr}, nil
	}
	u, err := url.Parse(proxyStr)
	if err != nil || u.Host == "" || u.Scheme == "" {
		return nil, fmt.Errorf("代理地址填错了：%s（要写成 http://IP:端口 或 socks5://IP:端口）", maskProxy(proxyStr))
	}
	tr.Proxy = http.ProxyURL(u)
	return &http.Client{Timeout: timeout, Transport: tr}, nil
}

// maskProxy 避免把代理里的账号密码写进日志或页面错误。
func maskProxy(proxyStr string) string {
	u, err := url.Parse(proxyStr)
	if err != nil || u.User == nil {
		return proxyStr
	}
	if _, ok := u.User.Password(); ok {
		u.User = url.UserPassword(u.User.Username(), "***")
	} else {
		u.User = url.User(u.User.Username())
	}
	return u.String()
}
