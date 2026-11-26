// WxIpManager：检测公网 IP + 扫码登录企微后台 + 自动改「企业可信 IP」。
// 端口对齐 app/wxip.py 的 WxIpManager：
//
//   - 所有浏览器操作只在 run_forever 这一条 goroutine 里做（网页接口
//     只置 jobs 标志 + 读缓存状态，chromedp 的 session 不能并发用）；
//   - 登录态（cookie 集合）存 DATA_DIR/wxip/state.json，重启后按
//     「已登录」显示，真失效了下一轮检查会自己变 expired；
//   - 通知走注入的 Notifier（notify 插件），没接也能用，只是不推送。
package core

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Notifier 是通知插件暴露给本插件的最小接口（避免 import 环，
// 方法集与 notify.Notifier.Send 一致）。
type Notifier interface {
	Send(text, event, level string, force bool) map[string]any
}

// loginState 取值：idle / qr / waiting / verify / ok / expired / error。
// verify = 企微登录触发了安全验证，等待用户在页面填验证码。
const (
	LoginIdle    = "idle"
	LoginQR      = "qr"
	LoginWaiting = "waiting"
	LoginVerify  = "verify"
	LoginOK      = "ok"
	LoginExpired = "expired"
	LoginError   = "error"
)

// Manager 管理器。
type Manager struct {
	dataDir  string
	store    *KV
	notifier Notifier
	dir      string // DATA_DIR/wxip

	mu         sync.Mutex
	jobs       map[string]bool // login / update / shot
	loginState string
	qrB64      string
	qrAt       time.Time
	verifyB64  string // 验证码区域截图（data URL），前端展示
	verifyMsg  string // 给用户的提示（如「已点获取验证码，请查收短信」）
	verifyCode string // 用户提交的验证码，登录轮询里消费
	verifyAt   time.Time

	browserState      string // 浏览器状态：ready / installing / failed / unsupported
	browserMsg        string
	browserAt         time.Time
	browserInstalling bool

	currentIP  string
	ipSource   string
	lastCheck  time.Time
	lastUpdate map[string]any
	lastError  string
	lastShot   string

	nextCheck time.Time
}

// New 建管理器（浏览器到用的时候才起）。
func New(dataDir string, st *KV, notifier Notifier) *Manager {
	return &Manager{
		dataDir:    dataDir,
		store:      st,
		notifier:   notifier,
		dir:        filepath.Join(dataDir, "wxip"),
		jobs:       map[string]bool{"login": false, "update": false, "shot": false},
		loginState: LoginIdle,
		lastUpdate: map[string]any{},
	}
}

// Settings 读配置。
func (m *Manager) Settings() Settings { return Load(m.store) }

// SaveSettings 保存配置并立刻按新间隔走下一轮。
func (m *Manager) SaveSettings(patch map[string]any) Settings {
	cur := Save(m.store, patch)
	m.mu.Lock()
	m.nextCheck = time.Time{} // 清零 = 马上按新值走一轮
	m.mu.Unlock()
	log.Printf("[wxip] 企微可信IP 配置已更新（启用=%v 自动=%v 间隔=%ds）",
		cur.Enabled, cur.Auto, cur.Interval)
	return cur
}

// Status 汇总页面要展示的全部状态（契约对齐 Python status()）。
func (m *Manager) Status() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	hist := History(m.store)
	if hist == nil {
		hist = []map[string]any{}
	}
	return map[string]any{
		"settings":    m.Settings(),
		"available":   ChromiumAvailable(),
		"login":       m.loginState,
		"qr":          m.qrB64,
		"qr_at":       qrAtUnix(m.qrAt),
		"verify":      m.verifyB64,
		"verify_msg":  m.verifyMsg,
		"verify_at":   qrAtUnix(m.verifyAt),
		"browser":       m.browserState,
		"browser_msg":   m.browserMsg,
		"browser_at":    qrAtUnix(m.browserAt),
		"logged_in":   m.stateExists(),
		"ip":          m.currentIP,
		"ip_source":   m.ipSource,
		"last_check":  unixSec(m.lastCheck),
		"last_update": cloneMap(m.lastUpdate),
		"last_error":  m.lastError,
		"shot":        m.lastShot,
		"history":     hist,
		"hint":        m.hintLocked(),
	}
}

func (m *Manager) hintLocked() string {
	if ChromiumAvailable() {
		return ""
	}
	// 正在下载中：别再提示「没找到浏览器」让用户困惑，下载状态条会说明
	if m.browserState == BrowserInstalling {
		return "浏览器正在首次下载中，完成后即可扫码登录，无需手动操作。"
	}
	if m.browserState == BrowserFailed || m.browserState == BrowserUnsupported {
		return "" // 失败原因已在浏览器状态条里详细展示
	}
	return "没找到 Chromium/Chrome 浏览器内核，正在准备自动下载；也可设 CHROME_PATH 指定可执行文件"
}

func qrAtUnix(t time.Time) float64 {
	if t.IsZero() {
		return 0
	}
	return float64(t.Unix())
}

func unixSec(t time.Time) float64 {
	if t.IsZero() {
		return 0
	}
	return float64(t.Unix())
}

func cloneMap(src map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range src {
		out[k] = v
	}
	return out
}

// stateFile 是登录态文件路径。
func (m *Manager) stateFile() string { return filepath.Join(m.dir, "state.json") }

func (m *Manager) stateExists() bool {
	_, err := os.Stat(m.stateFile())
	return err == nil
}

// ------------------------------------------------------- 网页触发的动作

// RequestLogin 开始扫码登录（后台跑，前端轮询 /api/wxip 取二维码）。
func (m *Manager) RequestLogin() map[string]any {
	m.mu.Lock()
	m.jobs["login"] = true
	m.nextCheck = time.Time{}
	m.mu.Unlock()
	return map[string]any{"ok": true, "login": m.loginStateLocked()}
}

// RequestVerifyCode 提交用户填的验证码（网页 API 调用，只置标志，
// 真正的填表提交由 loginFlow 在浏览器 goroutine 里完成）。
func (m *Manager) RequestVerifyCode(code string) map[string]any {
	code = strings.TrimSpace(code)
	if code == "" {
		return map[string]any{"ok": false, "detail": "验证码不能为空"}
	}
	m.mu.Lock()
	m.verifyCode = code
	m.mu.Unlock()
	return map[string]any{"ok": true}
}

// takeVerifyCode 原子取走待提交的验证码（登录轮询里调用）。
func (m *Manager) takeVerifyCode() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	c := m.verifyCode
	m.verifyCode = ""
	return c
}

// setVerify 记录验证码截图并切到 verify 状态（登录 goroutine 调用）。
func (m *Manager) setVerify(img, msg string) {
	m.mu.Lock()
	m.verifyB64 = img
	m.verifyMsg = msg
	m.verifyAt = time.Now()
	m.loginState = LoginVerify
	m.mu.Unlock()
}

// clearVerify 清空验证码相关状态。
func (m *Manager) clearVerify() {
	m.mu.Lock()
	m.verifyB64 = ""
	m.verifyMsg = ""
	m.verifyCode = ""
	m.mu.Unlock()
}

// RequestUpdate 手动强制更新一轮。
func (m *Manager) RequestUpdate() map[string]any {
	if !m.stateExists() {
		return map[string]any{"ok": false, "detail": "还没登录企微管理后台，先扫码"}
	}
	m.mu.Lock()
	m.jobs["update"] = true
	m.mu.Unlock()
	return map[string]any{"ok": true}
}

// RequestShot 诊断截图。
func (m *Manager) RequestShot() map[string]any {
	m.mu.Lock()
	m.jobs["shot"] = true
	m.mu.Unlock()
	return map[string]any{"ok": true}
}

// Logout 删除登录态。
func (m *Manager) Logout() map[string]any {
	if err := os.Remove(m.stateFile()); err != nil && !os.IsNotExist(err) {
		return map[string]any{"ok": false, "detail": err.Error()}
	}
	m.mu.Lock()
	m.loginState = LoginIdle
	m.qrB64 = ""
	m.verifyB64 = ""
	m.verifyMsg = ""
	m.verifyCode = ""
	m.mu.Unlock()
	return map[string]any{"ok": true}
}

// loginStateLocked 是无锁快照（只给返回值拼装用）。
func (m *Manager) loginStateLocked() string { return m.loginState }

// ---------------------------------------------------------------- 通知

func (m *Manager) notify(text, level string) {
	if m.notifier == nil || !m.Settings().Notify {
		return
	}
	go func() {
		defer func() { _ = recover() }()
		_ = m.notifier.Send(text, "wxip", level, false)
	}()
}

// ------------------------------------------------------------ 后台循环

// RunForever 守护循环（2 秒一拍，消费 jobs + 定时检测）。
func (m *Manager) RunForever(stop <-chan struct{}) {
	// 启动时磁盘上还有登录态就先按「已登录」显示，真失效下一轮自会变 expired
	if m.stateExists() {
		m.mu.Lock()
		if m.loginState == LoginIdle {
			m.loginState = LoginOK
		}
		m.mu.Unlock()
	}
	s := m.Settings()
	log.Printf("[wxip] 企微可信IP 守护已启动（启用=%v 自动=%v 浏览器=%v）",
		s.Enabled, s.Auto, ChromiumAvailable())
	// 浏览器自检/按需下载（异步，绝不阻塞主服务 10 秒健康检查）
	m.ensureBrowser()
	for {
		select {
		case <-stop:
			return
		default:
		}
		func() {
			defer func() {
				if r := recover(); r != nil {
					m.setError(fmt.Sprintf("panic: %v", r))
					log.Printf("[wxip] 企微可信IP 任务异常: %v", r)
				}
			}()
			m.tick(stop)
		}()
		select {
		case <-stop:
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func (m *Manager) setError(msg string) {
	m.mu.Lock()
	m.lastError = msg
	m.mu.Unlock()
}

// takeJob 原子地取走一个 job 标志。
func (m *Manager) takeJob(name string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.jobs[name] {
		m.jobs[name] = false
		return true
	}
	return false
}

func (m *Manager) tick(stop <-chan struct{}) {
	s := m.Settings()
	switch {
	case m.takeJob("login"):
		m.loginFlow(stop, s)
	case m.takeJob("shot"):
		m.snapshot()
	case m.takeJob("update"):
		m.checkAndUpdate(stop, s, true)
		m.mu.Lock()
		m.nextCheck = time.Now().Add(time.Duration(s.Interval) * time.Second)
		m.mu.Unlock()
		return
	}
	if !s.Enabled {
		return
	}
	m.mu.Lock()
	due := m.nextCheck.IsZero() || !time.Now().Before(m.nextCheck)
	if due {
		m.nextCheck = time.Now().Add(time.Duration(s.Interval) * time.Second)
	}
	m.mu.Unlock()
	if due {
		m.checkAndUpdate(stop, s, false)
	}
}

// checkAndUpdate 检测 IP，变了（或强制）就进后台改可信 IP。
func (m *Manager) checkAndUpdate(stop <-chan struct{}, s Settings, force bool) {
	ip, source := DetectIP(s.Proxy)
	m.mu.Lock()
	m.lastCheck = time.Now()
	m.mu.Unlock()
	if ip == "" {
		m.setError("所有 IP 检测接口都不通（检查一下容器的网络）")
		log.Printf("[wxip] 公网 IP 检测失败")
		return
	}
	m.mu.Lock()
	old := m.currentIP
	m.currentIP = ip
	m.ipSource = source
	m.lastError = ""
	m.mu.Unlock()

	if !force && old != "" && old == ip {
		return
	}
	if !force && !s.Auto {
		if old != "" && old != ip {
			m.notify(fmt.Sprintf("公网 IP 变了：%s → %s（自动更新已关闭，需要手动点一下）", old, ip), "info")
		}
		return
	}
	if !m.stateExists() {
		if force {
			m.setLastUpdate(map[string]any{"at": float64(time.Now().Unix()), "ok": false, "detail": "还没扫码登录"})
			m.setLogin(LoginExpired)
			return
		}
		// 定时检查发现 IP 变了但还没登录：提示一次，别每轮都刷
		if old != "" && old != ip {
			m.setLogin(LoginExpired)
			m.notify("公网 IP 变了，但还没登录企微后台，没法自动改可信IP", "warn")
		}
		return
	}
	result := m.updateAgents(stop, s, AgentList(s.AgentIDs), ip)
	m.setLastUpdate(map[string]any{
		"at": float64(time.Now().Unix()), "ip": ip, "old": old,
		"ok": result["ok"], "detail": strOf(result["detail"]),
	})
	PushHistory(m.store, map[string]any{
		"at": float64(time.Now().Unix()), "ip": ip, "old": old,
		"ok": result["ok"], "detail": strOf(result["detail"]),
	})
	if truthy(result["ok"]) {
		m.setLogin(LoginOK)
		if old != "" && old != ip {
			m.notify(fmt.Sprintf("企微可信IP 已更新：%s → %s", old, ip), "info")
		}
	} else {
		if strings.Contains(strOf(result["detail"]), "登录") {
			m.setLogin(LoginExpired)
		}
		m.notify(fmt.Sprintf("企微可信IP 更新失败：%s", strOf(result["detail"])), "err")
	}
}

func (m *Manager) setLastUpdate(v map[string]any) {
	m.mu.Lock()
	m.lastUpdate = v
	m.mu.Unlock()
}

func (m *Manager) setLogin(state string) {
	m.mu.Lock()
	m.loginState = state
	m.mu.Unlock()
}

// ------------------------------------------------------------- 截图诊断

func (m *Manager) snapshot() {
	var shot string
	err := m.withSession(90*time.Second, m.loadState(), func(b *browser) error {
		if err := b.open(FRAME_URL, 3*time.Second); err != nil {
			return err
		}
		data, err := b.screenshotPNG()
		if err != nil {
			return err
		}
		shot = pngDataURL(data)
		return nil
	})
	if err != nil {
		m.setError(fmt.Sprintf("截图失败：%v", err))
		return
	}
	m.mu.Lock()
	m.lastShot = shot
	m.mu.Unlock()
}

// stateJSON 是 state.json 的磁盘格式（对齐 playwright storage_state 的
// cookies 字段名，方便将来互换）。
type stateJSON struct {
	Cookies []cookieJSON `json:"cookies"`
}

type cookieJSON struct {
	Name     string  `json:"name"`
	Value    string  `json:"value"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"`
	HTTPOnly bool    `json:"httpOnly"`
	Secure   bool    `json:"secure"`
}

func (m *Manager) saveState(cookies []cookieJSON) error {
	if err := os.MkdirAll(m.dir, 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(stateJSON{Cookies: cookies}, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(m.stateFile(), raw, 0o600)
}

func (m *Manager) loadState() []cookieJSON {
	raw, err := os.ReadFile(m.stateFile())
	if err != nil {
		return nil
	}
	var st stateJSON
	if json.Unmarshal(raw, &st) != nil {
		return nil
	}
	return st.Cookies
}
