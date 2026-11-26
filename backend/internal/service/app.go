// Package service 是业务层：账号、STRM 生成、分享转存、ed2k、秒传、反代、通知、联动删除。
package service

import (
	"context"
	"fmt"
	"log"
	"net/url"
	"strings"
	"sync"
	"time"

	"panshare/internal/config"
	"panshare/internal/db"
	"panshare/internal/model"
	"panshare/internal/p115"
	"panshare/internal/strm"
)

// App 把各服务串在一起。所有长任务都从这里拿依赖。
type App struct {
	Cfg   *config.Config
	DB    *db.DB
	Store *strm.Store

	settingsMu sync.RWMutex
	settings   model.Settings

	clientsMu sync.Mutex
	clients   map[string]*p115.Client

	qrMu   sync.Mutex
	qrSess map[string]*qrSession

	copyMu    sync.Mutex
	copyCache map[string]copyEntry
	sessions  map[string]map[string]time.Time

	genMu   sync.Mutex
	running map[string]bool

	TG        *Telegram
	TMDB      *TMDB
	WeChat    *WeChat
	WeChatMP  *WeChatMP
	WeChatApp *WeChatApp
	Plugins   *Plugins
	Proxy     *EmbyProxy

	stop   chan struct{}
	stopMu sync.Once
	wg     sync.WaitGroup

	// 后台任务状态，仪表盘用
	startedAt time.Time
}

type qrSession struct {
	qr      *p115.QR
	expires time.Time
	// 给已有账号重新登录时带上
	accountID string
}

type copyEntry struct {
	pickcode string
	expire   time.Time
}

func NewApp(cfg *config.Config, database *db.DB, store *strm.Store) *App {
	s := database.GetSettings(model.DefaultSettings(cfg.StrmRoot, cfg.PublicBaseURL))
	a := &App{
		Cfg:       cfg,
		DB:        database,
		Store:     store,
		settings:  s,
		clients:   map[string]*p115.Client{},
		qrSess:    map[string]*qrSession{},
		copyCache: map[string]copyEntry{},
		sessions:  map[string]map[string]time.Time{},
		running:   map[string]bool{},
		stop:      make(chan struct{}),
		startedAt: time.Now(),
	}
	a.TG = &Telegram{app: a}
	a.TMDB = &TMDB{app: a, images: map[string]tmdbImage{}}
	a.WeChat = &WeChat{app: a}
	a.WeChatMP = &WeChatMP{app: a}
	a.WeChatApp = &WeChatApp{app: a}
	a.Plugins = NewPlugins(a)
	a.Proxy = NewEmbyProxy(a)
	return a
}

func (a *App) StartedAt() time.Time { return a.startedAt }

// Settings 返回当前设置的副本。
func (a *App) Settings() model.Settings {
	a.settingsMu.RLock()
	defer a.settingsMu.RUnlock()
	return a.settings
}

// SaveSettings 保存设置，并按需重建客户端（QPS 变了要重新限速）。
func (a *App) SaveSettings(s model.Settings) error {
	if err := a.DB.SaveSettings(s); err != nil {
		return err
	}
	a.settingsMu.Lock()
	old := a.settings
	a.settings = s
	a.settingsMu.Unlock()

	// 映射根可以在「服务设置」里改，改完立即重建 Store（后续写入落到新根）
	rootChanged := false
	if s.StrmRoot != "" && s.StrmRoot != old.StrmRoot && s.StrmRoot != a.Store.Root {
		if st, err := strm.New(s.StrmRoot); err == nil {
			a.Store = st
			rootChanged = true
			a.Event("info", "settings", fmt.Sprintf("映射根已切换到 %s", st.Root), "")
		}
	}

	a.clientsMu.Lock()
	for _, c := range a.clients {
		c.SetQPS(s.APIQPS)
	}
	a.clientsMu.Unlock()

	// 插件直接往映射根写 strm、按请求头拼播放地址，这两项变了要让它重起一次
	if rootChanged || s.PublicBaseURL != old.PublicBaseURL {
		if a.Plugins != nil {
			go a.Plugins.RestartRunning()
		}
	}

	// 反代端口或开关改了，就在原地重绑 / 停掉监听，不用重启容器
	if (s.EmbyProxyPort != old.EmbyProxyPort || s.EmbyProxyOff != old.EmbyProxyOff) && a.Proxy != nil {
		a.Proxy.Reload()
	}
	return nil
}

// BaseURL 是写进 strm 的请求头。
func (a *App) BaseURL() string {
	base := strings.TrimRight(a.Settings().PublicBaseURL, "/")
	if base == "" {
		base = strings.TrimRight(a.Cfg.PublicBaseURL, "/")
	}
	return base
}

// PlayURL 拼 strm 内容：<请求头>/p115/play/<pickcode>/<名字>/<115 用户 ID>。
func (a *App) PlayURL(pickcode, name, uid string) string {
	return a.PlayURLWith("", pickcode, name, uid)
}

// PlayURLWith 用指定请求头拼播放地址；base 为空时用全局设置。
// 每条生成配置可以自己填请求头，方便「家里走 192.168、外面走域名」。
func (a *App) PlayURLWith(base, pickcode, name, uid string) string {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if base == "" {
		base = a.BaseURL()
	}
	u := fmt.Sprintf("%s/p115/play/%s", base, url.PathEscape(pickcode))
	if name != "" {
		u += "/" + url.PathEscape(name)
	}
	u += "/" + url.PathEscape(uid)
	return u
}

// SharePlayURL 拼分享通道的播放地址（strm 指向自己的长期分享）。
func (a *App) SharePlayURL(shareCode, fileID, name, receiveCode string) string {
	u := fmt.Sprintf("%s/p115/share/%s/%s", a.BaseURL(),
		url.PathEscape(shareCode), url.PathEscape(fileID))
	if name != "" {
		u += "/" + url.PathEscape(name)
	}
	if receiveCode != "" {
		u += "?rc=" + url.QueryEscape(receiveCode)
	}
	return u
}

// Ed2kPlayURL 拼 ed2k 播放地址：文件被清理后重新转存 pickcode 会变，
// 只有 /p115/ed2k/<hash> 是稳定的。
func (a *App) Ed2kPlayURL(hash, name string) string {
	u := fmt.Sprintf("%s/p115/ed2k/%s", a.BaseURL(), url.PathEscape(hash))
	if name != "" {
		u += "/" + url.PathEscape(name)
	}
	return u
}

// Event 记录一条事件并异步推送到 Telegram / 微信。
// 所有后台任务都用它，页面「事件」和通知因此天然一致。
func (a *App) Event(level, kind, message, detail string) {
	if len(message) > 900 {
		message = message[:900] + "…"
	}
	a.DB.AddEvent(model.Event{Level: level, Kind: kind, Message: message, Detail: detail})
	log.Printf("[%s] %s: %s", level, kind, message)
	go a.TG.Notify(level, message)
	go a.WeChat.Notify(level, kind, message)
	go a.WeChatMP.Notify(level, message)
}

// client 返回账号的 115 客户端（带缓存）。
func (a *App) client(accountID string) (*p115.Client, error) {
	id := accountID
	if id == "" {
		acc := a.DB.DefaultAccount()
		if acc == nil {
			return nil, fmt.Errorf("还没有 115 账号，请先到「115 助手」添加")
		}
		id = acc.ID
	}
	a.clientsMu.Lock()
	defer a.clientsMu.Unlock()
	if c, ok := a.clients[id]; ok {
		return c, nil
	}
	acc, err := a.DB.GetAccount(id)
	if err != nil {
		return nil, err
	}
	if acc == nil {
		return nil, fmt.Errorf("账号不存在")
	}
	cookies := a.DB.Cookies(id)
	if cookies == "" {
		return nil, fmt.Errorf("账号「%s」没有登录态，请重新扫码或粘贴 Cookie", accountLabel(acc))
	}
	c := p115.New(cookies)
	c.SetQPS(a.Settings().APIQPS)
	a.clients[id] = c
	return c, nil
}

// clientByUID 按 115 用户 ID 找客户端（strm 里只有 uid）。
func (a *App) clientByUID(uid string) (*p115.Client, string, error) {
	// strm 里带的 uid 可能是纯数字，也可能是老版本写进去的整条 UID cookie，
	// 统一剪成前面的数字再找账号。
	uid = p115.CleanUID(uid)
	if uid != "" {
		if acc, err := a.DB.GetAccountByUID(uid); err == nil && acc != nil {
			c, err := a.client(acc.ID)
			return c, acc.ID, err
		}
		for _, acc := range a.DB.ListAccounts() {
			if p115.CleanUID(acc.UID) == uid {
				c, err := a.client(acc.ID)
				return c, acc.ID, err
			}
		}
	}
	acc := a.DB.DefaultAccount()
	if acc == nil {
		return nil, "", fmt.Errorf("还没有 115 账号")
	}
	c, err := a.client(acc.ID)
	return c, acc.ID, err
}

func (a *App) dropClient(accountID string) {
	a.clientsMu.Lock()
	delete(a.clients, accountID)
	a.clientsMu.Unlock()
}

func accountLabel(a *model.Account) string {
	if a == nil {
		return ""
	}
	if a.Name != "" {
		return a.Name
	}
	if a.UserName != "" {
		return a.UserName
	}
	if a.UID != "" {
		return "115-" + a.UID
	}
	return a.ID
}

// ---------------------------------------------------------------- 账号

// RefreshAccount 校验登录态并补全 uid / 昵称。
func (a *App) RefreshAccount(ctx context.Context, id string) (*model.Account, error) {
	acc, err := a.DB.GetAccount(id)
	if err != nil || acc == nil {
		return nil, fmt.Errorf("账号不存在")
	}
	cookies := a.DB.Cookies(id)
	if cookies == "" {
		return nil, fmt.Errorf("账号没有登录态")
	}
	c := p115.New(cookies)
	c.SetQPS(a.Settings().APIQPS)
	uid, name, err := c.UserInfo(ctx)
	if err != nil {
		_ = a.DB.UpdateAccount(id, map[string]any{"status": "error", "last_error": err.Error()})
		return nil, err
	}
	fields := map[string]any{"status": "ok", "last_error": "", "uid": uid}
	if name != "" {
		fields["user_name"] = name
	}
	if acc.Name == "" {
		if name != "" {
			fields["name"] = name
		} else if uid != "" {
			fields["name"] = "115-" + uid[len(uid)-4:]
		}
	}
	if err := a.DB.UpdateAccount(id, fields); err != nil {
		return nil, err
	}
	a.dropClient(id)
	return a.DB.GetAccount(id)
}

// AddAccountByCookie 用 cookies 添加（或更新）账号。
func (a *App) AddAccountByCookie(ctx context.Context, raw, name string) (*model.Account, error) {
	cookies, err := p115.NormalizeCookies(raw)
	if err != nil {
		return nil, err
	}
	c := p115.New(cookies)
	c.SetQPS(a.Settings().APIQPS)
	uid, userName, err := c.UserInfo(ctx)
	if err != nil {
		return nil, fmt.Errorf("这份 Cookie 用不了：%w", err)
	}
	if uid == "" {
		return nil, fmt.Errorf("没能读到 115 用户 ID，请确认复制的是登录后的完整 Cookie")
	}
	var id string
	existing, _ := a.DB.GetAccountByUID(uid)
	if existing != nil {
		id = existing.ID
	} else {
		id = "a" + uid
	}
	acc := model.Account{
		ID: id, Name: name, UID: uid, UserName: userName, Source: "cookie",
		Status: "ok", CreatedAt: time.Now().Unix(),
	}
	if acc.Name == "" {
		acc.Name = userName
	}
	if acc.Name == "" {
		acc.Name = "115-" + uid
	}
	if a.DB.DefaultAccount() == nil {
		acc.IsDefault = true
	}
	if existing != nil {
		acc.Name = firstNonEmptyStr(name, existing.Name)
		acc.CopyPlay = existing.CopyPlay
	}
	if err := a.DB.UpsertAccount(acc, cookies); err != nil {
		return nil, err
	}
	a.dropClient(id)
	a.Event("success", "account", fmt.Sprintf("账号已保存：%s（uid %s）", acc.Name, uid), "")
	return a.DB.GetAccount(id)
}

func firstNonEmptyStr(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// StartQR 生成登录二维码。
func (a *App) StartQR(ctx context.Context, accountID string) (map[string]any, error) {
	c := p115.New("")
	qr, err := c.QRToken(ctx)
	if err != nil {
		return nil, err
	}
	a.qrMu.Lock()
	// 清掉过期会话，避免内存里堆垃圾
	for k, s := range a.qrSess {
		if time.Now().After(s.expires) {
			delete(a.qrSess, k)
		}
	}
	a.qrSess[qr.UID] = &qrSession{qr: qr, expires: time.Now().Add(4 * time.Minute), accountID: accountID}
	a.qrMu.Unlock()
	return map[string]any{
		"uid": qr.UID, "content": qr.Content, "expires_in": 240,
	}, nil
}

// PollQR 轮询扫码结果，确认后落库。
func (a *App) PollQR(ctx context.Context, uid, name string) (map[string]any, error) {
	a.qrMu.Lock()
	sess, ok := a.qrSess[uid]
	a.qrMu.Unlock()
	if !ok {
		return nil, fmt.Errorf("二维码已过期，请刷新")
	}
	if time.Now().After(sess.expires) {
		return map[string]any{"status": "expired"}, nil
	}
	c := p115.New("")
	state, cookies, err := c.QRStatus(ctx, sess.qr)
	if err != nil {
		return nil, err
	}
	if state != "confirmed" {
		return map[string]any{"status": state}, nil
	}
	c2 := p115.New(cookies)
	c2.SetQPS(a.Settings().APIQPS)
	userID, userName, err := c2.UserInfo(ctx)
	if err != nil || userID == "" {
		return nil, fmt.Errorf("扫码已确认，但读取账号信息失败：%v", err)
	}
	id := sess.accountID
	if id == "" {
		if existing, _ := a.DB.GetAccountByUID(userID); existing != nil {
			id = existing.ID
		} else {
			id = "a" + userID
		}
	}
	acc := model.Account{
		ID: id, Name: firstNonEmptyStr(name, userName), UID: userID, UserName: userName,
		Source: "qrcode", Status: "ok", CreatedAt: time.Now().Unix(),
	}
	if acc.Name == "" {
		acc.Name = "115-" + userID
	}
	if a.DB.DefaultAccount() == nil {
		acc.IsDefault = true
	}
	if old, _ := a.DB.GetAccount(id); old != nil {
		acc.CopyPlay = old.CopyPlay
		acc.Name = firstNonEmptyStr(name, old.Name)
	}
	if err := a.DB.UpsertAccount(acc, cookies); err != nil {
		return nil, err
	}
	a.dropClient(id)
	a.qrMu.Lock()
	delete(a.qrSess, uid)
	a.qrMu.Unlock()
	a.Event("success", "account", fmt.Sprintf("扫码登录成功：%s（uid %s）", acc.Name, userID), "")
	acc2, _ := a.DB.GetAccount(id)
	return map[string]any{"status": "ok", "account": acc2}, nil
}

// UpdateAccount 修改备注名 / 同播复制开关 / 默认账号。
func (a *App) UpdateAccount(id string, fields map[string]any) error {
	allowed := map[string]bool{"name": true, "copy_play": true}
	clean := map[string]any{}
	for k, v := range fields {
		if allowed[k] {
			clean[k] = v
		}
	}
	if err := a.DB.UpdateAccount(id, clean); err != nil {
		return err
	}
	a.dropClient(id)
	return nil
}

// DeleteAccount 删除账号；名下配置一并删除（strm 保留），避免出现悬空配置。
func (a *App) DeleteAccount(id string) (int, error) {
	acc, _ := a.DB.GetAccount(id)
	if acc == nil {
		return 0, fmt.Errorf("账号不存在")
	}
	n := 0
	for _, c := range a.DB.ListConfigs() {
		if c.AccountID == id {
			if err := a.DB.DeleteConfig(c.ID); err == nil {
				n++
			}
		}
	}
	for _, t := range a.DB.ListShares() {
		if t.AccountID == id {
			_ = a.DB.DeleteShare(t.ID)
		}
	}
	_ = a.DB.DeleteInstantTarget(id)
	if err := a.DB.DeleteAccount(id); err != nil {
		return n, err
	}
	a.dropClient(id)
	if a.DB.DefaultAccount() == nil {
		if accs := a.DB.ListAccounts(); len(accs) > 0 {
			_ = a.DB.SetDefaultAccount(accs[0].ID)
		}
	}
	a.Event("info", "account", fmt.Sprintf("已删除账号：%s（同时移除 %d 条生成配置，本地 strm 保留）", accountLabel(acc), n), "")
	return n, nil
}

// ---------------------------------------------------------------- 目录浏览

// ListDirs 列云端目录，供前端目录选择器使用。
func (a *App) ListDirs(ctx context.Context, accountID, cid string) (map[string]any, error) {
	c, err := a.client(accountID)
	if err != nil {
		return nil, err
	}
	if cid == "" {
		cid = "0"
	}
	nodes, err := c.ListDir(ctx, cid)
	if err != nil {
		return nil, err
	}
	dirs := make([]map[string]any, 0, 32)
	files := 0
	for _, n := range nodes {
		if n.IsDir {
			dirs = append(dirs, map[string]any{"id": n.ID, "name": n.Name})
		} else {
			files++
		}
	}
	var pathName string
	if cid != "0" {
		if n, err := c.Details(ctx, cid); err == nil && n != nil {
			pathName = n.Name
		}
	}
	return map[string]any{"cid": cid, "dirs": dirs, "file_count": files, "name": pathName}, nil
}
