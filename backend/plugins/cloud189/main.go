package main

import (
	"context"
	"embed"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

//go:embed ui.html
var uiFS embed.FS

var (
	strmRoot  string
	baseURL   string
	playPrefx string
	store     *Store
)

type Manager struct {
	st    *Store
	mu    sync.Mutex
	links map[string]linkEntry // 直链缓存：播放器一次播放会打好几次 Range，别每次都去换链
}

type linkEntry struct {
	url string
	at  time.Time
}

// linkTTL 直链缓存时长。天翼的直链带签名和时效，120 秒足够覆盖一次起播，
// 又不会把过期地址留给下一次播放。
const linkTTL = 120 * time.Second

// defaultExts 默认生成 strm 的扩展名，页面上「媒体文件扩展名」留空时用它。
const defaultExts = "mp4,mkv,ts,m2ts,mts,iso,avi,wmv,flv,mov,rmvb"

// defaultMetaExts 默认的元数据扩展名：nfo、海报、字幕。打开「下载元数据文件」后，
// 这些文件直接落到 strm 同目录，Emby 会读同名的 nfo / 封面 / 外挂字幕。
const defaultMetaExts = "nfo,jpg,jpeg,png,srt,ass,ssa,sub,idx,vtt,txt"

// resolveLink 取文件直链，命中缓存就直接返回。
func (m *Manager) resolveLink(accountID, familyID, fileID string) (string, error) {
	key := accountID + "|" + familyID + "|" + fileID
	m.mu.Lock()
	if e, ok := m.links[key]; ok && time.Since(e.at) < linkTTL {
		m.mu.Unlock()
		return e.url, nil
	}
	m.mu.Unlock()

	c, _, err := m.client(accountID)
	if err != nil {
		return "", err
	}
	link, err := downloadURL(c, fileID, familyID)
	if err != nil {
		return "", err
	}
	m.mu.Lock()
	if len(m.links) > 2000 { // 长时间运行兜底，避免无限膨胀
		m.links = map[string]linkEntry{}
	}
	m.links[key] = linkEntry{url: link, at: time.Now()}
	m.mu.Unlock()
	return link, nil
}

func (m *Manager) client(id string) (*CloudClient, *Account, error) {
	acc := m.st.GetAccount(id)
	if acc == nil {
		return nil, nil, fmt.Errorf("没有这个账号")
	}
	if acc.AccessToken == "" {
		return nil, nil, fmt.Errorf("账号「%s」还没登录", acc.Name)
	}
	// 快到期就先续（天翼没有 refreshToken，只能用 sessionKey 换）
	if !acc.AutoRefreshOff && acc.ExpiresAt > 0 &&
		time.Until(time.UnixMilli(acc.ExpiresAt)) < time.Hour && acc.SessionKey != "" {
		_, _ = m.refresh(acc.ID)
		acc = m.st.GetAccount(acc.ID)
	}
	c := &CloudClient{AccessToken: acc.AccessToken, ExpiresAt: acc.ExpiresAt}
	c.Refresh = func() (string, error) { return m.refresh(acc.ID) }
	return c, acc, nil
}

func (m *Manager) refresh(id string) (string, error) {
	acc := m.st.GetAccount(id)
	if acc == nil {
		return "", fmt.Errorf("账号不存在")
	}
	if acc.SessionKey == "" {
		return "", fmt.Errorf("没有会话密钥，无法自动续期，请重新登录")
	}
	token, expires, err := tokenBySessionKey(acc.SessionKey)
	if err != nil {
		m.st.AddEvent("续期", "err", fmt.Sprintf("%s 续期失败：%v", acc.Name, err))
		return "", err
	}
	m.st.UpdateToken(acc.ID, token, "", expires)
	m.st.AddEvent("续期", "info", fmt.Sprintf("%s token 已续期", acc.Name))
	// 老数据里可能存的是「天翼-<随机串>」这种占位名，续期成功后顺手补上真名
	if strings.HasPrefix(acc.Name, "天翼-") {
		if fresh := m.st.GetAccount(acc.ID); fresh != nil {
			fresh.Name = "" // 清掉占位名，让 fillProfile 重新取
			fillProfile(fresh)
			if fresh.Name != acc.Name {
				m.st.UpsertAccount(*fresh)
			}
		}
	}
	return token, nil
}

// autoRefreshOnce 把「剩不到 24 小时」的账号续一遍。
func (m *Manager) autoRefreshOnce() {
	for _, a := range m.st.Snapshot().Accounts {
		if a.AutoRefreshOff || a.SessionKey == "" || a.AccessToken == "" || a.ExpiresAt == 0 {
			continue
		}
		if time.Until(time.UnixMilli(a.ExpiresAt)) > 24*time.Hour {
			continue
		}
		if _, err := m.refresh(a.ID); err != nil {
			log.Printf("自动续期失败 %s: %v", a.Name, err)
		}
	}
}

// autoRefreshLoop 每 30 分钟扫一遍；启动 20 秒后先跑一次，重启容器就能把快过期的补上。
func (m *Manager) autoRefreshLoop(ctx context.Context) {
	timer := time.NewTimer(20 * time.Second)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return
	case <-timer.C:
		m.autoRefreshOnce()
	}
	t := time.NewTicker(30 * time.Minute)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			m.autoRefreshOnce()
		}
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, code int, err error) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": err.Error()})
}

func main() {
	addr := flag.String("addr", "127.0.0.1:18100", "监听地址")
	dataDir := flag.String("data", "./data", "数据目录")
	flag.StringVar(&strmRoot, "strm-root", "/strm", "strm 映射根")
	flag.StringVar(&baseURL, "base-url", "http://127.0.0.1:9527", "写进 strm 的请求头")
	flag.StringVar(&playPrefx, "play-prefix", "/api/plugins/cloud189/proxy", "播放路径前缀")
	flag.Parse()

	st, err := OpenStore(*dataDir)
	if err != nil {
		log.Fatalf("打开数据目录失败: %v", err)
	}
	store = st
	m := &Manager{st: st, links: map[string]linkEntry{}}
	go m.autoRefreshLoop(context.Background())

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		data, _ := uiFS.ReadFile("ui.html")
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		// 页面里内联了 JS，别让浏览器拿旧副本（升级后还跑着旧的轮询路径）
		w.Header().Set("Cache-Control", "no-store")
		w.Write(data)
	})
	mux.HandleFunc("GET /api/state", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, m.snapshot())
	})

	// ---------------------------------------------------------------- 登录
	mux.HandleFunc("POST /api/login/qr", func(w http.ResponseWriter, r *http.Request) {
		uuid, content, err := qrInit()
		if err != nil {
			fail(w, 502, err)
			return
		}
		// 二维码在本地算成 SVG 一起回：以前前端拿外部二维码服务拼 <img>，
		// 内网/DNS 一抽风图就空白，看着像「二维码弹出来又没了」。
		writeJSON(w, map[string]any{
			"uuid": uuid, "content": content,
			"svg": qrSVG(content), "size": len(qrRows(content)),
		})
	})
	// uuid 放 query 而不是路径段：新版接口下发的 uuid 是一整条 https://... 地址，
	// 塞进路径会带出 "//"，Go 的 ServeMux 会先 301 折叠成 "/" 再匹配（必然 404）。
	mux.HandleFunc("GET /api/login/qr", func(w http.ResponseWriter, r *http.Request) {
		uuid := r.URL.Query().Get("uuid")
		if uuid == "" {
			fail(w, 400, fmt.Errorf("缺少 uuid"))
			return
		}
		token, expires, sk, ok, err := qrPoll(uuid)
		if err != nil {
			fail(w, 502, err)
			return
		}
		if !ok {
			writeJSON(w, map[string]any{"status": "waiting"})
			return
		}
		acc := Account{ID: "c" + randomID(), AccessToken: token, SessionKey: sk,
			ExpiresAt: expires, LoginAt: time.Now().Unix()}
		fillProfile(&acc)
		m.st.UpsertAccount(acc)
		m.st.AddEvent("登录", "info", "扫码登录成功："+acc.Name)
		writeJSON(w, map[string]any{"status": "ok", "account": acc.Name, "id": acc.ID})
	})
	// 扫码卡住时看这个：最近 40 次轮询 189 到底回了什么（已脱敏）
	mux.HandleFunc("GET /api/login/debug", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"log": recentPolls()})
	})
	mux.HandleFunc("POST /api/login/cookie", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			SessionKey string `json:"session_key"`
			Name       string `json:"name"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		sk, err := parseSessionKey(body.SessionKey)
		if err != nil {
			fail(w, 400, err)
			return
		}
		token, expires, err := tokenBySessionKey(sk)
		if err != nil {
			fail(w, 502, err)
			return
		}
		acc := Account{ID: "c" + randomID(), AccessToken: token, SessionKey: sk,
			ExpiresAt: expires, Name: body.Name, LoginAt: time.Now().Unix()}
		fillProfile(&acc)
		m.st.UpsertAccount(acc)
		m.st.AddEvent("登录", "info", "Cookie 登录成功："+acc.Name)
		writeJSON(w, map[string]any{"ok": true, "account": acc.Name})
	})
	// 手动填 token：没有会话密钥，到期只能重新填
	mux.HandleFunc("POST /api/login/token", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Token     string `json:"token"`
			ExpiresIn int64  `json:"expires_in"`
			Name      string `json:"name"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		token := strings.TrimSpace(body.Token)
		if token == "" {
			fail(w, 400, fmt.Errorf("token 不能为空"))
			return
		}
		c := &CloudClient{AccessToken: token}
		if _, err := c.get("/open/user/getUserInfo.action", url.Values{}); err != nil {
			fail(w, 502, fmt.Errorf("这个 token 用不了：%v", err))
			return
		}
		acc := Account{ID: "c" + randomID(), AccessToken: token, Name: body.Name,
			ExpiresAt: normalizeExpiry(body.ExpiresIn), LoginAt: time.Now().Unix()}
		fillProfile(&acc)
		m.st.UpsertAccount(acc)
		m.st.AddEvent("登录", "info", "手动 token 登录成功："+acc.Name)
		writeJSON(w, map[string]any{"ok": true, "account": acc.Name})
	})

	// ---------------------------------------------------------------- 账号
	mux.HandleFunc("POST /api/accounts/{id}/default", func(w http.ResponseWriter, r *http.Request) {
		if !m.st.SetDefault(r.PathValue("id")) {
			fail(w, 404, fmt.Errorf("账号不存在"))
			return
		}
		writeJSON(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /api/accounts/{id}/refresh", func(w http.ResponseWriter, r *http.Request) {
		if _, err := m.refresh(r.PathValue("id")); err != nil {
			fail(w, 502, err)
			return
		}
		writeJSON(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /api/accounts/{id}/autorefresh", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Enabled bool `json:"enabled"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if !m.st.SetAutoRefresh(r.PathValue("id"), body.Enabled) {
			fail(w, 404, fmt.Errorf("账号不存在"))
			return
		}
		word := "关闭"
		if body.Enabled {
			word = "开启"
		}
		m.st.AddEvent("账号", "info", "已"+word+"自动续期")
		writeJSON(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /api/accounts/{id}/logout", func(w http.ResponseWriter, r *http.Request) {
		if !m.st.Logout(r.PathValue("id")) {
			fail(w, 404, fmt.Errorf("账号不存在"))
			return
		}
		m.st.AddEvent("账号", "info", "账号已退出登录（配置保留）")
		writeJSON(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("DELETE /api/accounts/{id}", func(w http.ResponseWriter, r *http.Request) {
		m.st.DeleteAccount(r.PathValue("id"))
		m.st.AddEvent("账号", "warn", "账号及其配置已删除")
		writeJSON(w, map[string]any{"ok": true})
	})

	// ---------------------------------------------------------------- 目录 / 配置
	mux.HandleFunc("GET /api/dirs", func(w http.ResponseWriter, r *http.Request) {
		c, _, err := m.client(r.URL.Query().Get("account_id"))
		if err != nil {
			fail(w, 400, err)
			return
		}
		ref := r.URL.Query().Get("ref")
		scope, familyID, folderID := parseRef(ref)
		if scope == "family_list" {
			fams, err := listFamilies(c)
			if err != nil {
				fail(w, 502, err)
				return
			}
			dirs := make([]map[string]any, 0, len(fams))
			for _, f := range fams {
				dirs = append(dirs, map[string]any{
					"id": "fam:" + fmt.Sprint(f["family_id"]), "name": f["name"],
				})
			}
			writeJSON(w, map[string]any{"dirs": dirs, "ref": ref})
			return
		}
		nodes, err := listFiles(c, folderID, familyID)
		if err != nil {
			fail(w, 502, err)
			return
		}
		dirs := make([]map[string]any, 0, len(nodes))
		for _, n := range nodes {
			if !n.IsDir {
				continue
			}
			id := n.ID
			if familyID != "" {
				id = "fam:" + familyID + ":" + n.ID
			}
			dirs = append(dirs, map[string]any{"id": id, "name": n.Name})
		}
		if familyID == "" && folderID == rootFolderID {
			dirs = append([]map[string]any{{"id": familyNode, "name": "家庭云"}}, dirs...)
		}
		writeJSON(w, map[string]any{"dirs": dirs, "ref": ref})
	})
	mux.HandleFunc("POST /api/configs", func(w http.ResponseWriter, r *http.Request) {
		var c Config
		if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
			fail(w, 400, err)
			return
		}
		c.Name = strings.TrimSpace(c.Name)
		c.LocalDir = strings.Trim(strings.TrimSpace(c.LocalDir), "/")
		c.BaseURL = strings.TrimSpace(c.BaseURL)
		if c.Name == "" || c.AccountID == "" {
			fail(w, 400, fmt.Errorf("配置名称和账号都要填"))
			return
		}
		if c.BaseURL != "" && !strings.HasPrefix(c.BaseURL, "http://") && !strings.HasPrefix(c.BaseURL, "https://") {
			fail(w, 400, fmt.Errorf("STRM 请求头要带 http:// 或 https://"))
			return
		}
		if c.ID == "" {
			c.ID = "g" + randomID()
			m.st.AddEvent("配置", "info", "新增配置："+c.Name)
		} else {
			if old := m.st.GetConfig(c.ID); old != nil {
				// 编辑时保留上次运行结果，别把「已生成 N 个」清空
				c.Files = old.Files
				c.LastRunAt = old.LastRunAt
				c.LastMessage = old.LastMessage
			}
			m.st.AddEvent("配置", "info", "修改配置："+c.Name)
		}
		if strings.TrimSpace(c.IncludeExts) == "" {
			c.IncludeExts = defaultExts
		}
		if strings.TrimSpace(c.MetaExts) == "" {
			c.MetaExts = defaultMetaExts
		}
		m.st.SaveConfig(c)
		writeJSON(w, map[string]any{"ok": true, "config": c})
	})
	// 本地目录选择：列 strm 根下的子目录，页面上「选择」按钮用
	mux.HandleFunc("GET /api/locals", func(w http.ResponseWriter, r *http.Request) {
		rel := strings.Trim(strings.TrimSpace(r.URL.Query().Get("path")), "/")
		dir := filepath.Join(strmRoot, filepath.FromSlash(rel))
		// 只允许在映射根里面翻，别让 ../ 把整台机器都列出来
		if inside, err := filepath.Rel(strmRoot, dir); err != nil || strings.HasPrefix(inside, "..") {
			fail(w, 400, fmt.Errorf("路径不合法"))
			return
		}
		entries, err := os.ReadDir(dir)
		if err != nil && !os.IsNotExist(err) {
			fail(w, 500, err)
			return
		}
		dirs := []string{}
		for _, e := range entries {
			if e.IsDir() && !strings.HasPrefix(e.Name(), ".") {
				dirs = append(dirs, e.Name())
			}
		}
		writeJSON(w, map[string]any{"path": rel, "dirs": dirs})
	})
	mux.HandleFunc("DELETE /api/configs/{id}", func(w http.ResponseWriter, r *http.Request) {
		if cfg := m.st.GetConfig(r.PathValue("id")); cfg != nil {
			m.st.AddEvent("配置", "warn", "删除配置："+cfg.Name)
		}
		m.st.DeleteConfig(r.PathValue("id"))
		writeJSON(w, map[string]any{"ok": true})
	})
	// mode: inc（只补缺）/ full（变了就重写）/ rebuild（先删再全量）
	mux.HandleFunc("POST /api/configs/{id}/run", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		mode := r.URL.Query().Get("mode")
		if mode != "inc" && mode != "rebuild" {
			mode = "full"
		}
		cfg := m.st.GetConfig(id)
		if cfg == nil {
			fail(w, 404, fmt.Errorf("配置不存在"))
			return
		}
		name := cfg.Name
		m.st.AddEvent("生成", "info", fmt.Sprintf("开始生成：%s（%s）", name, modeText(mode)))
		go func() {
			if err := m.runConfig(id, mode); err != nil {
				log.Printf("生成失败 %s: %v", id, err)
				m.st.AddEvent("生成", "err", fmt.Sprintf("%s 生成失败：%v", name, err))
				if c := m.st.GetConfig(id); c != nil {
					c.LastMessage = "失败：" + err.Error()
					m.st.SaveConfig(*c)
				}
			}
		}()
		writeJSON(w, map[string]any{"ok": true, "accepted": true})
	})
	mux.HandleFunc("GET /api/events", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"events": m.st.Snapshot().Events})
	})
	mux.HandleFunc("DELETE /api/events", func(w http.ResponseWriter, r *http.Request) {
		m.st.ClearEvents()
		writeJSON(w, map[string]any{"ok": true})
	})

	mux.HandleFunc("GET /play/{account}/{file}", func(w http.ResponseWriter, r *http.Request) {
		m.play(w, r, r.PathValue("account"), "", r.PathValue("file"))
	})
	mux.HandleFunc("GET /play/{account}/fam/{family}/{file}", func(w http.ResponseWriter, r *http.Request) {
		m.play(w, r, r.PathValue("account"), r.PathValue("family"), r.PathValue("file"))
	})
	// 新生成的 strm 末尾会带文件名（播放器靠它认容器），最后一段只做展示，不参与取链。
	// 老的「不带文件名」地址继续有效，换版本不用重生成全部 strm。
	mux.HandleFunc("GET /play/{account}/{file}/{name...}", func(w http.ResponseWriter, r *http.Request) {
		m.play(w, r, r.PathValue("account"), "", r.PathValue("file"))
	})
	mux.HandleFunc("GET /play/{account}/fam/{family}/{file}/{name...}", func(w http.ResponseWriter, r *http.Request) {
		m.play(w, r, r.PathValue("account"), r.PathValue("family"), r.PathValue("file"))
	})

	log.Printf("天翼云盘插件已启动: %s（strm 根 %s）", *addr, strmRoot)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}

// playURL 拼一条 strm 内容：<请求头><前缀>/play/<账号>[/fam/<家庭id>]/<文件id>/<文件名>。
//
// 末尾的文件名只做展示（播放器靠后缀认容器），取链时只用到 fileID，
// 所以老的不带文件名的地址仍然能播，升级不用把全部 strm 重写一遍。
func playURL(base, prefix, acc, fam, fileID, name string) string {
	if base == "" {
		base = baseURL
	}
	u := strings.TrimRight(base, "/") + strings.TrimRight(prefix, "/") + "/play/" + acc
	if fam != "" {
		u += "/fam/" + fam
	}
	u += "/" + fileID
	if name != "" {
		u += "/" + url.PathEscape(name)
	}
	return u
}

func modeText(mode string) string {
	switch mode {
	case "inc":
		return "增量"
	case "rebuild":
		return "全新"
	default:
		return "全量"
	}
}

// snapshot 给页面用的完整状态：账号 + 配置 + 事件 + 顶部统计。
// 统计都在这里算好，前端不用自己拼。
func (m *Manager) snapshot() map[string]any {
	st := m.st.Snapshot()
	now := nowMS()

	cfgCount := map[string]int{}
	files := 0
	for _, c := range st.Configs {
		cfgCount[c.AccountID]++
		files += c.Files
	}
	accName := map[string]string{}
	online := 0
	var nextExpire int64
	accounts := make([]map[string]any, 0, len(st.Accounts))
	for _, a := range st.Accounts {
		accName[a.ID] = a.Name
		logged := a.AccessToken != ""
		if logged && (a.ExpiresAt == 0 || a.ExpiresAt > now) {
			online++
		}
		if logged && a.ExpiresAt > now && (nextExpire == 0 || a.ExpiresAt < nextExpire) {
			nextExpire = a.ExpiresAt
		}
		leftDays, leftHours := 0.0, 0.0
		if logged && a.ExpiresAt > now {
			leftDays = float64(a.ExpiresAt-now) / float64(24*time.Hour/time.Millisecond)
			leftDays = float64(int(leftDays*10)) / 10
			leftHours = float64(a.ExpiresAt-now) / float64(time.Hour/time.Millisecond)
			leftHours = float64(int(leftHours*10)) / 10
		}
		accounts = append(accounts, map[string]any{
			"id": a.ID, "name": a.Name, "login_name": a.LoginName,
			"expires_at": a.ExpiresAt, "is_default": a.IsDefault,
			"can_refresh": a.SessionKey != "", "logged_in": logged,
			"auto_refresh": !a.AutoRefreshOff, "configs": cfgCount[a.ID],
			"left_days": leftDays, "left_hours": leftHours, "login_at": a.LoginAt,
			"expired": logged && a.ExpiresAt > 0 && a.ExpiresAt <= now,
		})
	}
	configs := make([]map[string]any, 0, len(st.Configs))
	for _, c := range st.Configs {
		configs = append(configs, map[string]any{
			"id": c.ID, "name": c.Name, "account_id": c.AccountID,
			"account_name": accName[c.AccountID],
			"remote_ref":   c.RemoteRef, "remote_path": c.RemotePath,
			"local_dir": c.LocalDir, "include_exts": c.IncludeExts,
			"base_url": c.BaseURL, "no_filter": c.NoFilter,
			"download_meta": c.DownloadMeta, "meta_exts": c.MetaExts,
			"last_run_at": c.LastRunAt, "last_message": c.LastMessage,
			"files": c.Files, "is_family": strings.HasPrefix(c.RemoteRef, familyNode),
		})
	}
	return map[string]any{
		"accounts": accounts, "configs": configs, "events": st.Events,
		"strm_root": strmRoot, "base_url": baseURL,
		"play_prefix": strings.TrimRight(playPrefx, "/"),
		"ext_default": defaultExts, "meta_ext_default": defaultMetaExts,
		"stats": map[string]any{
			"accounts": len(st.Accounts), "online": online,
			"configs": len(st.Configs), "files": files, "next_expire": nextExpire,
		},
	}
}

// fillProfile 拉一次用户信息，补昵称 / 登录名（失败不影响登录）。
func fillProfile(acc *Account) {
	c := &CloudClient{AccessToken: acc.AccessToken}
	m, err := c.get("/open/user/getUserInfo.action", url.Values{})
	if err != nil {
		if acc.Name == "" {
			acc.Name = "天翼-" + acc.ID
		}
		return
	}
	acc.LoginName = firstOf(m, "loginName", "loginname")
	if acc.Name == "" {
		acc.Name = firstOf(m, "nickName", "nickname", "userName", "name", "email")
	}
	// 天翼这个接口经常只有 loginName，昵称是空的：用登录名当账号名，
	// 否则卡片上只能显示「天翼-<随机串>」，根本分不清是哪个号。
	if acc.Name == "" {
		acc.Name = acc.LoginName
	}
	if acc.Name == "" {
		acc.Name = "天翼-" + acc.ID
	}
}

func (m *Manager) play(w http.ResponseWriter, r *http.Request, accountID, familyID, fileID string) {
	link, err := m.resolveLink(accountID, familyID, fileID)
	if err != nil {
		http.Error(w, "解析直链失败: "+err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	http.Redirect(w, r, link, http.StatusFound)
}

// parseRef 解析目录引用："" / -11 个人云根，fam 家庭云列表，fam:<id>[:folder]。
func parseRef(ref string) (scope, familyID, folderID string) {
	ref = strings.TrimSpace(ref)
	if ref == "" || ref == "-11" {
		return "personal", "", rootFolderID
	}
	if ref == familyNode {
		return "family_list", "", ""
	}
	if strings.HasPrefix(ref, familyNode+":") {
		parts := strings.Split(ref, ":")
		if len(parts) > 1 {
			// 老版本可能把 familyId 写成科学计数法存下来，这里顺手修回整数
			familyID = fixNum(parts[1])
		}
		if len(parts) > 2 {
			folderID = parts[2]
		}
		return "family", familyID, folderID
	}
	return "personal", "", ref
}

func (m *Manager) runConfig(id, mode string) error {
	cfg := m.st.GetConfig(id)
	if cfg == nil {
		return fmt.Errorf("配置不存在")
	}
	c, acc, err := m.client(cfg.AccountID)
	if err != nil {
		return err
	}
	if mode == "rebuild" {
		// 只删这条配置自己的目录，别碰别人生成的 strm。
		// LocalDir 为空代表「直接放 strm 根目录」，这时清空等于删光所有人的 strm，
		// 所以宁可不删，也不能把整棵树扫了。
		if cfg.LocalDir == "" {
			log.Printf("配置 %s 的本地目录就是 strm 根目录，全新生成时不清空（避免误删别的配置）", cfg.Name)
		} else if n, err := clearStrm(filepath.Join(strmRoot, cfg.LocalDir)); err == nil {
			log.Printf("配置 %s 全新生成：先清掉 %d 个旧 strm", cfg.Name, n)
		}
	}
	scope, familyID, folderID := parseRef(cfg.RemoteRef)
	// 每条配置可以单独指定请求头；没填就用全局地址
	base := strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	if base == "" {
		base = strings.TrimRight(baseURL, "/")
	}
	prefix := strings.TrimRight(playPrefx, "/")
	exts := map[string]bool{}
	for _, p := range strings.FieldsFunc(cfg.IncludeExts, func(r rune) bool {
		return r == ',' || r == ';' || r == ' '
	}) {
		p = strings.ToLower(strings.TrimSpace(p))
		if p == "" {
			continue
		}
		if !strings.HasPrefix(p, ".") {
			p = "." + p
		}
		exts[p] = true
	}
	// 元数据扩展名集合（只在开关打开时才用得上）
	metaExts := map[string]bool{}
	for _, p := range strings.FieldsFunc(cfg.MetaExts, func(r rune) bool {
		return r == ',' || r == ';' || r == ' '
	}) {
		p = strings.ToLower(strings.TrimSpace(p))
		if p == "" {
			continue
		}
		if !strings.HasPrefix(p, ".") {
			p = "." + p
		}
		metaExts[p] = true
	}
	if len(metaExts) == 0 {
		for _, p := range strings.Split(defaultMetaExts, ",") {
			metaExts["."+strings.TrimSpace(p)] = true
		}
	}
	count, metaCount, metaFailed := 0, 0, 0

	var walk func(fid, fam, rel string, depth int) error
	walk = func(fid, fam, rel string, depth int) error {
		if depth > 32 {
			return nil
		}
		nodes, err := listFiles(c, fid, fam)
		if err != nil {
			return err
		}
		for _, n := range nodes {
			name := safeName(n.Name)
			relPath := filepath.Join(rel, name)
			if n.IsDir {
				if err := walk(n.ID, fam, relPath, depth+1); err != nil {
					return err
				}
				continue
			}
			ext := strings.ToLower(filepath.Ext(n.Name))
			isMedia := cfg.NoFilter || len(exts) == 0 || exts[ext]
			dst := filepath.Join(strmRoot, cfg.LocalDir, relPath)
			if !isMedia {
				// 元数据（nfo / 海报 / 字幕）不进 strm，直接放到 strm 所在目录，
				// Emby 才能按同名文件认出来
				if cfg.DownloadMeta && metaExts[ext] {
					if err := saveMetaFile(c, fam, n.ID, n.Size, dst); err != nil {
						metaFailed++
						log.Printf("配置 %s：下载元数据 %s 失败（%v）", cfg.Name, relPath, err)
					} else {
						metaCount++
					}
				}
				continue
			}
			// 末尾带上文件名：播放器靠它认容器和字幕，Emby 显示也更友好
			u := playURL(base, prefix, acc.ID, fam, n.ID, name)
			if !strings.HasSuffix(strings.ToLower(dst), ".strm") {
				dst += ".strm"
			}
			if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
				continue
			}
			if old, err := os.ReadFile(dst); err == nil {
				// 增量：已经有了就不动；全量：内容没变也不动
				if mode == "inc" || string(old) == u {
					count++
					continue
				}
			}
			if err := os.WriteFile(dst, []byte(u), 0o644); err != nil {
				continue
			}
			count++
		}
		return nil
	}
	switch scope {
	case "family_list":
		fams, err := listFamilies(c)
		if err != nil {
			return err
		}
		for _, f := range fams {
			fid := fmt.Sprint(f["family_id"])
			if err := walk(rootFolderID, fid, safeName(fmt.Sprint(f["name"])), 0); err != nil {
				return err
			}
		}
	case "family":
		if err := walk(rootFolderID, familyID, "", 0); err != nil {
			return err
		}
	default:
		if err := walk(folderID, "", "", 0); err != nil {
			return err
		}
	}
	cfg.Files = count
	cfg.LastRunAt = time.Now().Unix()
	msg := fmt.Sprintf("%s：%d 个 strm", modeText(mode), count)
	if cfg.DownloadMeta {
		msg += fmt.Sprintf(" · %d 个元数据", metaCount)
	}
	if metaFailed > 0 {
		msg += fmt.Sprintf(" · %d 个元数据失败", metaFailed)
	}
	cfg.LastMessage = msg
	m.st.SaveConfig(*cfg)
	m.st.AddEvent("生成", "info", fmt.Sprintf("%s 完成：%s", cfg.Name, msg))
	log.Printf("天翼云盘配置 %s 完成：%d 个文件", cfg.Name, count)
	return nil
}

// clearStrm 删掉某个目录下的 .strm（递归），返回删掉的个数。
func clearStrm(dir string) (int, error) {
	n := 0
	err := filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil || info == nil || info.IsDir() {
			return nil
		}
		if strings.EqualFold(filepath.Ext(p), ".strm") {
			if os.Remove(p) == nil {
				n++
			}
		}
		return nil
	})
	return n, err
}

func safeName(name string) string {
	r := strings.NewReplacer("/", "／", "\\", "＼", ":", "：", "*", "＊", "?", "？",
		"\"", "＂", "<", "＜", ">", "＞", "|", "｜")
	out := strings.TrimRight(strings.TrimSpace(r.Replace(name)), ". ")
	if out == "" {
		out = "未命名"
	}
	if len(out) > 180 {
		out = out[:180]
	}
	return out
}

// saveMetaFile 把云盘上的元数据文件（nfo / 海报 / 字幕）下载到本地 dst。
//
// dst 就是该文件「本来会生成的 strm」所在路径（去掉 .strm），所以落下来之后
// 正好和视频同名同目录，Emby 能直接读到。已存在且大小一致就跳过，增量不会重下。
func saveMetaFile(c *CloudClient, familyID, fileID string, size int64, dst string) error {
	const maxMetaSize = 200 << 20
	if size > maxMetaSize {
		return nil
	}
	if fi, err := os.Stat(dst); err == nil && (size <= 0 || fi.Size() == size) {
		return nil
	}
	link, err := downloadURL(c, fileID, familyID)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodGet, link, nil)
	if err != nil {
		return err
	}
	// 天翼 CDN 只认 UA，随便给个浏览器 UA 即可
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
	client := &http.Client{Timeout: 3 * time.Minute}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("下载返回 %d", resp.StatusCode)
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	tmp := dst + ".part"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	n, copyErr := io.Copy(f, io.LimitReader(resp.Body, maxMetaSize+1))
	closeErr := f.Close()
	if copyErr != nil || closeErr != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("写入失败: %v", copyErr)
	}
	if n > maxMetaSize {
		_ = os.Remove(tmp)
		return fmt.Errorf("文件超过 200MB，不当元数据处理")
	}
	return os.Rename(tmp, dst)
}
