// 浏览器自检与按需安装。
//
// 镜像里故意不装 chromium（体积约 600MB）。插件首次启动时异步检测：
// 系统里已有浏览器就直接用；没有就用国内 apk 源下载安装。
// 必须异步——主服务 Start() 只等 10 秒健康检查，同步下载会拖垮启动。
package core

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"
)

// 浏览器状态（前端据此显示「就绪 / 下载中 / 失败」）。
const (
	BrowserReady      = "ready"
	BrowserInstalling = "installing"
	BrowserFailed     = "failed"
	BrowserUnsupported = "unsupported"
)

// apkMirrors 国内优先，官方兜底（实测容器内都能通，USTC 不通故不列）。
// 每个 base 会拼出 main + community 两行（chromium 在 community）。
var apkMirrors = []string{
	"https://mirrors.aliyun.com/alpine",
	"https://mirrors.tuna.tsinghua.edu.cn/alpine",
	"https://dl-cdn.alpinelinux.org/alpine",
}

const apkReposBackup = "/etc/apk/repositories.wxip-bak"

// ensureBrowser 在 RunForever 启动时调用：有浏览器就标就绪，否则后台下载。
func (m *Manager) ensureBrowser() {
	if FindChromium() != "" {
		m.setBrowser(BrowserReady, "浏览器已就绪")
		return
	}
	if runtime.GOOS != "linux" {
		m.setBrowser(BrowserUnsupported, "当前系统未检测到浏览器，请手动安装 Chromium 或设置 CHROME_PATH")
		return
	}
	if _, err := exec.LookPath("apk"); err != nil {
		m.setBrowser(BrowserUnsupported, "系统没有 apk 包管理器，无法自动下载；请手动安装 Chromium 或设置 CHROME_PATH")
		return
	}
	go m.installChromium()
}

// installChromium 用国内 apk 源下载安装，成功即可。带并发保护防止重复下载。
func (m *Manager) installChromium() {
	m.mu.Lock()
	if m.browserInstalling {
		m.mu.Unlock()
		return
	}
	m.browserInstalling = true
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		m.browserInstalling = false
		m.mu.Unlock()
	}()

	ver, err := alpineMajorMinor()
	if err != nil {
		m.setBrowser(BrowserFailed, "读取 Alpine 版本失败："+err.Error())
		return
	}

	var lastErr error
	for _, base := range apkMirrors {
		repos := fmt.Sprintf("%s/v%s/main\n%s/v%s/community\n", base, ver, base, ver)
		m.setBrowser(BrowserInstalling, "正在从 "+base+" 下载浏览器（首次约几分钟）…")
		log.Printf("[wxip] 开始安装 chromium，源=%s", base)

		if err := withApkRepos(repos, func() error {
			cmd := exec.Command("apk", "add", "--no-cache", "chromium")
			out, err := cmd.CombinedOutput()
			if err != nil {
				return fmt.Errorf("%v: %s", err, tail(string(out), 300))
			}
			return nil
		}); err != nil {
			lastErr = err
			log.Printf("[wxip] 从 %s 安装 chromium 失败: %v", base, err)
			continue
		}
		if exe := FindChromium(); exe != "" {
			m.setBrowser(BrowserReady, "浏览器已就绪（"+exe+"）")
			log.Printf("[wxip] chromium 安装完成：%s", exe)
			return
		}
		lastErr = fmt.Errorf("apk 安装返回成功但没找到浏览器可执行文件")
	}
	msg := "浏览器下载失败：" + strOf(lastErr) + "。可手动安装 Chromium 或设置 CHROME_PATH"
	m.setBrowser(BrowserFailed, msg)
	log.Printf("[wxip] %s", msg)
}

// RequestInstallBrowser 供前端手动重试下载。
func (m *Manager) RequestInstallBrowser() map[string]any {
	if FindChromium() != "" {
		m.setBrowser(BrowserReady, "浏览器已就绪")
		return map[string]any{"ok": true, "state": BrowserReady}
	}
	go m.installChromium()
	return map[string]any{"ok": true, "state": BrowserInstalling}
}

// withApkRepos 临时替换 apk 源，执行 fn 后恢复（避免污染系统配置）。
var apkRepoMu sync.Mutex

func withApkRepos(repos string, fn func() error) error {
	apkRepoMu.Lock()
	defer apkRepoMu.Unlock()
	const path = "/etc/apk/repositories"
	original, readErr := os.ReadFile(path)
	if readErr != nil {
		return fmt.Errorf("读取 %s 失败: %w", path, readErr)
	}
	if err := os.WriteFile(apkReposBackup, original, 0o644); err != nil {
		return fmt.Errorf("备份 apk 源失败: %w", err)
	}
	if err := os.WriteFile(path, []byte(repos), 0o644); err != nil {
		return fmt.Errorf("写入国内 apk 源失败: %w", err)
	}
	err := fn()
	// 无论成功失败都恢复原源
	if rb, rerr := os.ReadFile(apkReposBackup); rerr == nil {
		_ = os.WriteFile(path, rb, 0o644)
	}
	_ = os.Remove(apkReposBackup)
	return err
}

func alpineMajorMinor() (string, error) {
	raw, err := os.ReadFile("/etc/alpine-release")
	if err != nil {
		return "", err
	}
	parts := strings.Split(strings.TrimSpace(string(raw)), ".")
	if len(parts) < 2 {
		return "", fmt.Errorf("无法解析版本号 %q", string(raw))
	}
	return parts[0] + "." + parts[1], nil
}

func tail(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return "...\n" + s[len(s)-n:]
}

func (m *Manager) setBrowser(state, msg string) {
	m.mu.Lock()
	m.browserState = state
	m.browserMsg = msg
	m.browserAt = time.Now()
	m.mu.Unlock()
}
