package service

import (
	"context"
	"encoding/json"
	"fmt"
	"hash/crc32"
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Telegram 通知 + 指令。
//
// 任务开始 / 完成 / 失败都会推送；开启了指令后，可以直接在聊天窗口里
// 发 /status、/scan、/find 等命令操作本服务。
//
// 国内机器直连 api.telegram.org 一定失败（DNS 还被污染成 208.43.x.x），
// 所以走共享代理：页面上的 tg_proxy（现为 Telegram / TMDB 共用）优先，
// 其次 compose 的 PS_PROXY，最后才是环境变量 HTTPS_PROXY。
type Telegram struct {
	app *App

	mu       sync.Mutex
	client   *http.Client
	proxyKey string // 建 client 时用的代理串，变了要重建
	proxyErr string // 代理串解析失败的原因（页面上能直接看到）

	lastErr   string
	lastErrAt time.Time
	lastOK    time.Time

	cmdSum    string    // 上次注册成功的命令表指纹
	cmdFailAt time.Time // 上次注册失败的时间，用来退避，别每 5 秒刷一次日志
	cmdFailN  int       // 连续失败次数：前几次快速重试（容器刚起来时网络还没就绪）
}

func (t *Telegram) enabled() bool {
	s := t.app.Settings()
	return s.TGEnabled && strings.TrimSpace(s.TGBotToken) != "" && strings.TrimSpace(s.TGChatID) != ""
}

func (t *Telegram) apiURL(method string) string {
	s := t.app.Settings()
	return fmt.Sprintf("https://api.telegram.org/bot%s/%s", strings.TrimSpace(s.TGBotToken), method)
}

// httpClient 按当前代理设置返回（必要时重建）HTTP 客户端。
func (t *Telegram) httpClient() *http.Client {
	proxyStr := t.app.SharedProxy()

	t.mu.Lock()
	defer t.mu.Unlock()
	if t.client != nil && t.proxyKey == proxyStr {
		return t.client
	}

	client, err := newProxyClient(proxyStr, 20*time.Second)
	t.proxyErr = ""
	if err != nil {
		t.proxyErr = err.Error()
		// 代理串写错时仍返回可用 client（走环境变量），但页面会显示错误原因
		client, _ = newProxyClient("", 20*time.Second)
	}
	t.client = client
	t.proxyKey = proxyStr
	return t.client
}

// scrub 把错误文本里的 bot token 抹掉。
//
// 网络错误会把整条 URL 带出来（Post "https://api.telegram.org/bot<token>/…": EOF），
// 这条错误会写进日志、也会显示在「通知」页上，token 就此泄露给所有能看日志的人。
func (t *Telegram) scrub(err error) error {
	if err == nil {
		return nil
	}
	tok := strings.TrimSpace(t.app.Settings().TGBotToken)
	msg := err.Error()
	if tok != "" {
		msg = strings.ReplaceAll(msg, tok, "<token>")
	}
	return fmt.Errorf("%s", msg)
}

func (t *Telegram) setErr(err error) {
	if err == nil {
		return
	}
	err = t.scrub(err)
	t.mu.Lock()
	t.lastErr = err.Error()
	t.lastErrAt = time.Now()
	t.mu.Unlock()
}

func (t *Telegram) setOK() {
	t.mu.Lock()
	t.lastOK = time.Now()
	t.lastErr = ""
	t.lastErrAt = time.Time{}
	t.mu.Unlock()
}

// Notify 推送一条消息。没配置就静默跳过（不刷日志）。
// 失败只记在 lastErr 里（页面「通知」能看到），不写事件——事件本身会触发推送，会打转。
func (t *Telegram) Notify(level, message string) {
	if !t.enabled() {
		return
	}
	icon := map[string]string{
		"success": "✅", "info": "ℹ️", "warn": "⚠️", "error": "❌",
	}[level]
	if icon == "" {
		icon = "•"
	}
	t.setErr(t.send(t.app.Settings().TGChatID, icon+" "+message))
}

// Test 同步发一条测试消息，把真实错误原样返回给页面。
func (t *Telegram) Test() error {
	s := t.app.Settings()
	if strings.TrimSpace(s.TGBotToken) == "" {
		return fmt.Errorf("还没填 Bot Token")
	}
	if strings.TrimSpace(s.TGChatID) == "" {
		return fmt.Errorf("还没填 Chat ID")
	}
	err := t.send(s.TGChatID, "✅ PanShare 测试消息：通知链路正常")
	t.setErr(err)
	if err != nil {
		return err
	}
	t.setOK()
	return nil
}

func (t *Telegram) send(chatID, text string) error {
	c := t.httpClient()
	t.mu.Lock()
	perr := t.proxyErr
	t.mu.Unlock()
	if perr != "" {
		return fmt.Errorf("%s", perr)
	}

	form := url.Values{}
	form.Set("chat_id", strings.TrimSpace(chatID))
	form.Set("text", text)
	form.Set("disable_web_page_preview", "true")
	resp, err := c.PostForm(t.apiURL("sendMessage"), form)
	if err != nil {
		return fmt.Errorf("连不上 api.telegram.org：%v（国内需在下面填代理）", t.scrub(err))
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
	if resp.StatusCode >= 300 {
		return fmt.Errorf("telegram HTTP %d：%s", resp.StatusCode, tgDescribe(body, resp.StatusCode))
	}
	if desc, ok := tgFailure(body); ok {
		return fmt.Errorf("telegram 拒绝了：%s", desc)
	}
	return nil
}

// tgFailure 从 {"ok":false,"description":"..."} 里取描述。
func tgFailure(body []byte) (string, bool) {
	var r struct {
		OK          bool   `json:"ok"`
		Description string `json:"description"`
	}
	if json.Unmarshal(body, &r) != nil {
		return "", false
	}
	if r.OK {
		return "", false
	}
	if r.Description == "" {
		return string(body), true
	}
	return r.Description, true
}

func tgDescribe(body []byte, status int) string {
	if d, ok := tgFailure(body); ok {
		return d
	}
	s := strings.TrimSpace(string(body))
	if s == "" {
		return fmt.Sprintf("HTTP %d（空响应）", status)
	}
	return s
}

// Start 启动指令轮询（只有开启指令时才真正发请求）。
func (t *Telegram) Start(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if s := t.app.Settings(); s.TGEnabled && s.TGCommands && strings.TrimSpace(s.TGBotToken) != "" {
				// 命令菜单（聊天框敲 / 弹出来的那份）跟着配置走，
				// 配置增删改名之后需要重新注册一次。
				t.syncCommandsIfChanged()
				t.poll(ctx)
			}
		}
	}
}

func (t *Telegram) poll(ctx context.Context) {
	offset := t.app.DB.GetMeta("tg_offset", "0")
	q := url.Values{}
	q.Set("offset", offset)
	q.Set("timeout", "0")
	q.Set("limit", "20")

	resp, err := t.httpClient().Get(t.apiURL("getUpdates") + "?" + q.Encode())
	if err != nil {
		t.setErr(fmt.Errorf("拉取 TG 消息失败：%v", err))
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	// webhook 没清掉时 getUpdates 会 409，指令永远没反应——顺手清一次
	if resp.StatusCode == http.StatusConflict && strings.Contains(string(body), "webhook") {
		if _, derr := t.httpClient().Get(t.apiURL("deleteWebhook")); derr == nil {
			t.app.Event("info", "telegram", "检测到 Bot 设了 webhook，已自动删除（否则收不到指令）", "")
			return
		}
	}

	var payload struct {
		OK          bool   `json:"ok"`
		Description string `json:"description"`
		Result      []struct {
			UpdateID int64 `json:"update_id"`
			Message  *struct {
				Text string `json:"text"`
				Chat struct {
					ID int64 `json:"id"`
				} `json:"chat"`
			} `json:"message"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		t.setErr(fmt.Errorf("telegram 返回看不懂：%s", tgDescribe(body, resp.StatusCode)))
		return
	}
	if !payload.OK {
		desc := payload.Description
		if desc == "" {
			desc = tgDescribe(body, resp.StatusCode)
		}
		if strings.Contains(desc, "terminated by other getUpdates") {
			desc += "（有另一个程序在用同一个 Bot Token 拉消息，比如别的机器人，先停掉它）"
		}
		t.setErr(fmt.Errorf("telegram 拒绝 getUpdates：%s", desc))
		return
	}
	t.setOK()

	maxID := offset
	for _, up := range payload.Result {
		maxID = strconv.FormatInt(up.UpdateID+1, 10)
		if up.Message == nil {
			continue
		}
		// 只响应配置里的 chat，别人拉进群也指挥不动
		if fmt.Sprint(up.Message.Chat.ID) != strings.TrimSpace(t.app.Settings().TGChatID) {
			continue
		}
		// 非命令的消息也交过去：这里面可能有 ed2k / 分享链接，要按设置生成 strm。
		// 普通聊天 command 会返回空串，不回复，群里不至于被机器人吵到。
		text := strings.TrimSpace(up.Message.Text)
		if text == "" {
			continue
		}
		if reply := t.command(text); reply != "" {
			t.setErr(t.sendLong(fmt.Sprint(up.Message.Chat.ID), reply))
		}
	}
	if maxID != offset {
		_ = t.app.DB.SetMeta("tg_offset", maxID)
	}
}

// Status 给页面看的运行态：为什么收不到消息，这里能直接读出来。
func (t *Telegram) Status() map[string]any {
	s := t.app.Settings()
	t.mu.Lock()
	lastErr, errAt, okAt, proxyErr := t.lastErr, t.lastErrAt, t.lastOK, t.proxyErr
	t.mu.Unlock()

	out := map[string]any{
		"enabled":     t.enabled(),
		"commands":    s.TGCommands,
		"proxy":       strings.TrimSpace(s.TGProxy),
		"proxy_error": proxyErr,
		"last_error":  lastErr,
		"polling":     s.TGEnabled && s.TGCommands && strings.TrimSpace(s.TGBotToken) != "",
	}
	if !errAt.IsZero() {
		out["last_error_at"] = errAt.Format("2006-01-02 15:04:05")
	}
	if !okAt.IsZero() {
		out["last_ok_at"] = okAt.Format("2006-01-02 15:04:05")
	}
	return out
}

// command 把一条消息变成回复文本。空串表示「不用回」。
//
// 以 / 开头按指令解析；否则只当作「可能带链接的聊天」，交给链接处理，
// 普通文字静默忽略。
func (t *Telegram) command(text string) string {
	return t.app.commandText(text)
}

// commandText 是真正的解析与执行，挂在 App 上以便企业微信回调复用
// （微信侧和 Telegram 是同一套指令，回复都走这里）。
func (a *App) commandText(text string) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return ""
	}
	if !strings.HasPrefix(text, "/") {
		return a.tgHandleLinks(text, "")
	}
	fields := strings.Fields(text)
	// /cmd@SomeBot 群聊里会自动带机器人用户名，去掉
	head := strings.TrimPrefix(strings.ToLower(strings.Split(fields[0], "@")[0]), "/")
	arg := strings.TrimSpace(strings.TrimPrefix(text, fields[0]))

	switch head {
	case "start", "help":
		return a.tgHelp()
	case "status":
		accs := a.DB.ListAccounts()
		cfgs := a.DB.ListConfigs()
		files := 0
		for _, c := range cfgs {
			files += a.DB.CountFiles(c.ID)
		}
		return fmt.Sprintf("账号 %d 个 / 生成配置 %d 条 / 索引文件 %d 个\n运行时长 %s",
			len(accs), len(cfgs), files, time.Since(a.StartedAt()).Round(time.Minute))
	case "accounts":
		var b strings.Builder
		for _, acc := range a.DB.ListAccounts() {
			fmt.Fprintf(&b, "• %s（uid %s）%s\n", accountLabel(&acc), acc.UID,
				map[string]string{"ok": "", "error": " [异常]"}[acc.Status])
		}
		if b.Len() == 0 {
			return "还没有 115 账号"
		}
		return strings.TrimRight(b.String(), "\n")
	case "strm", "configs":
		return a.tgConfigList(true)
	case "inc", "scan":
		return a.tgGen(arg, ModeInc)
	case "full":
		return a.tgGen(arg, ModeFull)
	case "ed2k":
		return a.tgHandleLinks(arg, "ed2k")
	case "share":
		return a.tgHandleLinks(arg, "share")
	case "find":
		if arg == "" {
			return "用法：/find <关键词>"
		}
		rows := a.DB.SearchFiles(arg, 10)
		if len(rows) == 0 {
			return "没有找到"
		}
		var b strings.Builder
		for _, r := range rows {
			fmt.Fprintf(&b, "• %s\n", r.RelPath)
		}
		return strings.TrimRight(b.String(), "\n")
	case "cleanup":
		return a.tgCleanupList()
	case "cleanup_ok":
		return a.tgCleanupAct(arg, true)
	case "cleanup_no":
		return a.tgCleanupAct(arg, false)
	case "link":
		if !a.Settings().DeleteEnabled {
			return "联动删除总开关没开"
		}
		go a.LinkScan(context.Background())
		return "已开始联动删除对账"
	}

	// 配置专属指令：/inc_<代号>、/full_<代号>
	if alias, ok := strings.CutPrefix(head, "inc_"); ok {
		return a.tgGenByAlias(alias, ModeInc)
	}
	if alias, ok := strings.CutPrefix(head, "full_"); ok {
		return a.tgGenByAlias(alias, ModeFull)
	}
	return "未知指令，发 /help 看可用指令"
}

// sendLong 分段发送。Telegram 单条上限 4096 字符，/help 带配置列表
// 很容易超；按行切开，别把一条消息拆成半个汉字。
func (t *Telegram) sendLong(chatID, text string) error {
	const limit = 3600
	runes := []rune(strings.TrimSpace(text))
	for len(runes) > 0 {
		cut := len(runes)
		if cut > limit {
			cut = limit
			for i := cut; i > limit/2; i-- {
				if runes[i-1] == '\n' {
					cut = i
					break
				}
			}
		}
		if err := t.send(chatID, strings.TrimSpace(string(runes[:cut]))); err != nil {
			return err
		}
		runes = runes[cut:]
	}
	return nil
}

// syncCommandsIfChanged 把指令表注册给 Telegram。
//
// 注册的是「在聊天框里敲 / 会弹出的菜单」，纯体验：不注册也不影响
// 直接敲指令。配置增删或改名会改变代号，所以比对指纹，变了才发请求；
// 失败退避 5 分钟，别让网络问题把日志刷满。
func (t *Telegram) syncCommandsIfChanged() {
	raw, err := json.Marshal(t.app.tgCommandTable())
	if err != nil {
		return
	}
	sum := fmt.Sprintf("%x", crc32.ChecksumIEEE(raw))

	t.mu.Lock()
	done, failAt, failN := t.cmdSum, t.cmdFailAt, t.cmdFailN
	t.mu.Unlock()
	if done == sum {
		return
	}
	// 失败就指数退避：10s → 20s → 40s → … → 5 分钟封顶。
	// 容器刚起来那几秒网络常常还没通（第一次基本必失败），而代理节点抽风时
	// TLS 握手也会被 RST；退避太狠会让菜单要等好几分钟才就位。
	if !failAt.IsZero() {
		wait := 5 * time.Minute
		if failN < 6 {
			wait = 10 * time.Second << (failN - 1)
			if wait > 5*time.Minute {
				wait = 5 * time.Minute
			}
		}
		if time.Since(failAt) < wait {
			return
		}
	}

	form := url.Values{}
	form.Set("commands", string(raw))
	resp, err := t.httpClient().PostForm(t.apiURL("setMyCommands"), form)
	if err != nil {
		t.markCmdFail("注册命令菜单失败（不影响指令本身）：%v", t.scrub(err))
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
	if resp.StatusCode >= 300 {
		t.markCmdFail("注册命令菜单失败：%s", tgDescribe(body, resp.StatusCode))
		return
	}
	if desc, bad := tgFailure(body); bad {
		t.markCmdFail("注册命令菜单被拒：%s", desc)
		return
	}
	t.mu.Lock()
	t.cmdSum = sum
	t.cmdFailAt = time.Time{}
	t.cmdFailN = 0
	t.mu.Unlock()
}

// markCmdFail 记一次注册失败（只打日志，不影响指令本身能不能用）。
//
// 前三次都打，之后每 10 次打一条：代理抽风时这个错误会一直重复，
// 刷满日志反而把真正有用的信息盖掉。
func (t *Telegram) markCmdFail(format string, args ...any) {
	t.mu.Lock()
	t.cmdFailAt = time.Now()
	t.cmdFailN++
	n := t.cmdFailN
	t.mu.Unlock()
	if n <= 3 || n%10 == 0 {
		log.Printf("telegram："+format+"（连续第 %d 次）", append(args, n)...)
	}
}
