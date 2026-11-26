package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

type Account struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	LoginName   string `json:"login_name"`
	AccessToken string `json:"access_token"`
	SessionKey  string `json:"session_key"`
	ExpiresAt   int64  `json:"expires_at"`
	IsDefault   bool   `json:"is_default"`
	CreatedAt   int64  `json:"created_at"`
	LoginAt     int64  `json:"login_at"`
	// 用反向字段：老数据没有这个键时默认就是「开自动续期」，
	// 否则加个新字段会把所有已有账号都变成手动续期。
	AutoRefreshOff bool `json:"auto_refresh_off"`
}

type Config struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	AccountID  string `json:"account_id"`
	RemoteRef  string `json:"remote_ref"`
	RemotePath string `json:"remote_path"`
	LocalDir   string `json:"local_dir"`
	// BaseURL 是这条配置写进 strm 的请求头（客户端能访问到的本服务地址），
	// 留空就用全局 baseURL —— 内外网切换时不用改每一条配置。
	BaseURL string `json:"base_url"`
	// NoFilter 打开后不再按扩展名筛，云盘里所有文件都生成 strm（含图片、字幕）。
	NoFilter    bool   `json:"no_filter"`
	IncludeExts string `json:"include_exts"`
	// DownloadMeta 打开后，元数据后缀（nfo / 海报 / 字幕）的文件不生成 strm，
	// 直接下载到该 strm 所在的本地目录，Emby 才能读到同名的 nfo 和封面。
	DownloadMeta bool   `json:"download_meta"`
	MetaExts     string `json:"meta_exts"`
	LastRunAt    int64  `json:"last_run_at"`
	LastMessage  string `json:"last_message"`
	Files        int    `json:"files"`
}

// Event 是页面上「事件日志」里的一行。原来只打日志到容器里，
// 用户在页面上看不到「刚才那次续期到底成没成」。
type Event struct {
	At    int64  `json:"at"`
	Kind  string `json:"kind"`  // 登录 / 续期 / 配置 / 生成
	Level string `json:"level"` // info / warn / err
	Text  string `json:"text"`
}

type state struct {
	Accounts []Account `json:"accounts"`
	Configs  []Config  `json:"configs"`
	Events   []Event   `json:"events"`
}

type Store struct {
	path string
	mu   sync.Mutex
	st   state
}

func OpenStore(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	s := &Store{path: filepath.Join(dir, "cloud189.json")}
	data, err := os.ReadFile(s.path)
	if err == nil {
		_ = json.Unmarshal(data, &s.st)
	}
	return s, nil
}

func (s *Store) saveLocked() error {
	data, _ := json.MarshalIndent(s.st, "", "  ")
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *Store) Snapshot() state {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := state{
		Accounts: make([]Account, len(s.st.Accounts)),
		Configs:  make([]Config, len(s.st.Configs)),
		Events:   make([]Event, len(s.st.Events)),
	}
	copy(out.Accounts, s.st.Accounts)
	copy(out.Configs, s.st.Configs)
	copy(out.Events, s.st.Events)
	return out
}

// AddEvent 记一条事件（新的在前，最多留 200 条）。
func (s *Store) AddEvent(kind, level, text string) {
	if text == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ev := Event{At: time.Now().Unix(), Kind: kind, Level: level, Text: text}
	s.st.Events = append([]Event{ev}, s.st.Events...)
	if len(s.st.Events) > 200 {
		s.st.Events = s.st.Events[:200]
	}
	_ = s.saveLocked()
}

func (s *Store) ClearEvents() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.st.Events = nil
	_ = s.saveLocked()
}

// SetDefault 把某个账号设为默认（其余全部取消）。
func (s *Store) SetDefault(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	found := false
	for i := range s.st.Accounts {
		s.st.Accounts[i].IsDefault = s.st.Accounts[i].ID == id
		if s.st.Accounts[i].ID == id {
			found = true
		}
	}
	if found {
		_ = s.saveLocked()
	}
	return found
}

// SetAutoRefresh 开关自动续期。
func (s *Store) SetAutoRefresh(id string, on bool) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.st.Accounts {
		if s.st.Accounts[i].ID == id {
			s.st.Accounts[i].AutoRefreshOff = !on
			_ = s.saveLocked()
			return true
		}
	}
	return false
}

// Logout 退出登录：清掉 token 与会话密钥，但保留账号卡片和它名下的配置，
// 方便以后重新登录接着用。
func (s *Store) Logout(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.st.Accounts {
		if s.st.Accounts[i].ID == id {
			s.st.Accounts[i].AccessToken = ""
			s.st.Accounts[i].SessionKey = ""
			s.st.Accounts[i].ExpiresAt = 0
			_ = s.saveLocked()
			return true
		}
	}
	return false
}

// UpdateToken 登录/续期后回写 token。
func (s *Store) UpdateToken(id, token, sessionKey string, expiresAt int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.st.Accounts {
		if s.st.Accounts[i].ID == id {
			s.st.Accounts[i].AccessToken = token
			if sessionKey != "" {
				s.st.Accounts[i].SessionKey = sessionKey
			}
			s.st.Accounts[i].ExpiresAt = expiresAt
			s.st.Accounts[i].LoginAt = time.Now().Unix()
			_ = s.saveLocked()
			return
		}
	}
}

func (s *Store) UpsertAccount(a Account) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if a.CreatedAt == 0 {
		a.CreatedAt = time.Now().Unix()
	}
	found := false
	for i := range s.st.Accounts {
		if s.st.Accounts[i].ID == a.ID {
			old := s.st.Accounts[i]
			if a.AccessToken == "" {
				a.AccessToken = old.AccessToken
			}
			if a.SessionKey == "" {
				a.SessionKey = old.SessionKey
			}
			// 登录/续期只关心 token，别把用户设过的开关和默认标记冲掉
			a.AutoRefreshOff = old.AutoRefreshOff
			a.IsDefault = old.IsDefault
			if a.LoginAt == 0 {
				a.LoginAt = old.LoginAt
			}
			if a.CreatedAt == 0 {
				a.CreatedAt = old.CreatedAt
			}
			s.st.Accounts[i] = a
			found = true
		}
	}
	if !found {
		if len(s.st.Accounts) == 0 {
			a.IsDefault = true
		}
		s.st.Accounts = append(s.st.Accounts, a)
	}
	_ = s.saveLocked()
}

func (s *Store) GetAccount(id string) *Account {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.st.Accounts {
		if s.st.Accounts[i].ID == id || (id == "" && s.st.Accounts[i].IsDefault) {
			cp := s.st.Accounts[i]
			return &cp
		}
	}
	if id == "" && len(s.st.Accounts) > 0 {
		cp := s.st.Accounts[0]
		return &cp
	}
	return nil
}

func (s *Store) DeleteAccount(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.st.Accounts[:0]
	for _, a := range s.st.Accounts {
		if a.ID != id {
			out = append(out, a)
		}
	}
	s.st.Accounts = out
	cfgs := s.st.Configs[:0]
	for _, c := range s.st.Configs {
		if c.AccountID != id {
			cfgs = append(cfgs, c)
		}
	}
	s.st.Configs = cfgs
	_ = s.saveLocked()
}

func (s *Store) SaveConfig(c Config) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.st.Configs {
		if s.st.Configs[i].ID == c.ID {
			s.st.Configs[i] = c
			_ = s.saveLocked()
			return
		}
	}
	s.st.Configs = append(s.st.Configs, c)
	_ = s.saveLocked()
}

func (s *Store) GetConfig(id string) *Config {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.st.Configs {
		if s.st.Configs[i].ID == id {
			cp := s.st.Configs[i]
			return &cp
		}
	}
	return nil
}

func (s *Store) DeleteConfig(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.st.Configs[:0]
	for _, c := range s.st.Configs {
		if c.ID != id {
			out = append(out, c)
		}
	}
	s.st.Configs = out
	_ = s.saveLocked()
}

func randomID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// ---------------------------------------------------------------- 登录

func qrInit() (string, string, error) {
	u := apiURL + "/family/manage/getQrCodeUUID.action"
	req, err := http.NewRequest("GET", u+"?"+clientSuffix().Encode(), nil)
	if err != nil {
		return "", "", err
	}
	for k, vs := range appKeySignature(u, "GET") {
		for _, v := range vs {
			req.Header.Set(k, v)
		}
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", acceptHeader)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	body := make([]byte, 0, 4096)
	buf := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buf)
		body = append(body, buf[:n]...)
		if err != nil {
			break
		}
	}
	m, err := parseBody(body)
	if err != nil {
		return "", "", err
	}
	if msg := respError(m); msg != "" {
		return "", "", fmt.Errorf("获取二维码失败: %s", msg)
	}
	uuid := firstOf(m, "uuid")
	if uuid == "" {
		return "", "", fmt.Errorf("获取二维码失败：响应里没有 uuid")
	}
	return uuid, qrContent(uuid), nil
}

// qrContent 是二维码里要编码的字符串。
// 新版接口下发的「uuid」本身就是一条登录跳转 URL（https://open.e.189.cn/...?paras=new_uuid=…），
// 这时必须原样编进二维码；再套一层 loginUrl.action 模板会拼出「URL 里塞 URL」的坏内容，扫了也进不去。
func qrContent(raw string) string {
	if strings.HasPrefix(strings.ToLower(raw), "http") {
		return raw
	}
	return "https://cloud.189.cn/api/portal/loginUrl.action?redirectURL=https://cloud.189.cn&uuid=" + url.QueryEscape(raw)
}

// qrPollUUID 是回传给 qrcodeLoginResult 的 uuid。
// 新版下发的跳转 URL 里，真正的 uuid 在 paras=new_uuid=<uuid> 中，原样回传会被当成非法参数。
func qrPollUUID(raw string) string {
	if !strings.HasPrefix(strings.ToLower(raw), "http") {
		return raw
	}
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	if paras := u.Query().Get("paras"); strings.HasPrefix(paras, "new_uuid=") {
		return strings.TrimPrefix(paras, "new_uuid=")
	}
	return raw
}

// ---------------------------------------------------------------- 扫码诊断日志

var (
	secretRe   = regexp.MustCompile(`(?i)"(access_?token|session_?key|e189accesstoken|token)"\s*:\s*"([^"]{4})[^"]*"`)
	pollLogMu  sync.Mutex
	pollLogSig string
)

func maskSecret(s string) string {
	return secretRe.ReplaceAllString(s, `"$1":"$2…"`)
}

func prefixOf(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// logPollBody 只在响应内容变化时打一行，避免 2 秒一条把日志刷满。
// 扫码登录卡住时，这行日志是唯一能看出「189 到底回了什么」的线索。
func logPollBody(body []byte) {
	text := strings.TrimSpace(string(body))
	pollLogMu.Lock()
	changed := text != pollLogSig
	pollLogSig = text
	pollLogMu.Unlock()
	if !changed {
		return
	}
	log.Printf("二维码轮询响应: %s", truncate(maskSecret(text), 400))
}

// ---------------------------------------------------------------- 扫码诊断

type pollRecord struct {
	At     int64  `json:"at"`
	UUID   string `json:"uuid"`
	Result string `json:"result"`
	Body   string `json:"body"`
}

var (
	pollHistMu sync.Mutex
	pollHist   []pollRecord
)

// recordPoll 留最近 40 次轮询结果，页面上能直接看到 189 回了什么，
// 不用去容器里翻日志。body 里的 token / sessionKey 先脱敏。
func recordPoll(uuid, result string, body []byte) {
	tail := uuid
	if len(tail) > 24 {
		tail = tail[len(tail)-24:]
	}
	rec := pollRecord{
		At:     nowMS(),
		UUID:   tail,
		Result: result,
		Body:   truncate(maskSecret(strings.TrimSpace(string(body))), 400),
	}
	pollHistMu.Lock()
	pollHist = append(pollHist, rec)
	if len(pollHist) > 40 {
		pollHist = pollHist[len(pollHist)-40:]
	}
	pollHistMu.Unlock()
}

func recentPolls() []pollRecord {
	pollHistMu.Lock()
	defer pollHistMu.Unlock()
	out := make([]pollRecord, len(pollHist))
	copy(out, pollHist)
	return out
}

// qrPoll 轮询：uuid → e189AccessToken → sessionKey → accessToken。
//
// uuid 有两种口径：服务端下发的原值（通常是整条跳转 URL）和从 URL 里拆出来的
// new_uuid。哪个才是接口认的没法事先确定，而且**口径不对时接口不报错，只会一直回
// QrCodeRollLoginFail**（表现就是「扫了没反应」），所以两个都得问：谁先给 token 用谁。
func qrPoll(raw string) (string, int64, string, bool, error) {
	cands := []string{qrPollUUID(raw)}
	if cands[0] != raw {
		cands = append(cands, raw)
	}
	var lastErr error
	for i, u := range cands {
		token, expires, sk, ok, err := qrPollOnce(u)
		if err != nil {
			lastErr = err
			if i == 0 && len(cands) > 1 {
				log.Printf("二维码轮询：uuid=%s… 报错（%v），换原始值再试", prefixOf(u, 10), err)
			}
			continue
		}
		if ok {
			if i > 0 {
				log.Printf("二维码轮询：拆出来的 uuid 一直没结果，改用原始值后成功")
			}
			return token, expires, sk, true, nil
		}
	}
	return "", 0, "", false, lastErr
}

func qrPollOnce(uuid string) (token string, expires int64, sk string, ok bool, err error) {
	u := apiURL + "/family/manage/qrcodeLoginResult.action"
	q := clientSuffix()
	q.Set("uuid", uuid)
	req, err := http.NewRequest("GET", u+"?"+q.Encode(), nil)
	if err != nil {
		return "", 0, "", false, err
	}
	for k, vs := range appKeySignature(u, "GET") {
		for _, v := range vs {
			req.Header.Set(k, v)
		}
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", acceptHeader)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", 0, "", false, err
	}
	defer resp.Body.Close()
	var payload map[string]any
	body, _ := readAll(resp.Body)
	logPollBody(body)
	status := "waiting"
	defer func() {
		if err != nil {
			status = "error: " + truncate(err.Error(), 80)
		}
		recordPoll(uuid, status, body)
	}()
	if err := json.Unmarshal(body, &payload); err != nil {
		m, perr := parseBody(body)
		if perr != nil {
			return "", 0, "", false, perr
		}
		payload = m
	}
	e189 := firstOf(payload, "accessToken", "access_token", "e189AccessToken", "token")
	if e189 == "" {
		// 扫完码但接口不回 token、只回一个跳转地址：说明走的是网页登录通道，
		// 需要再拿 ticket 换 token，光等下去永远不会成功，得把原因说清楚。
		if jump := firstOf(payload, "toUrl", "redirectUrl", "url", "location"); jump != "" {
			return "", 0, "", false, fmt.Errorf("扫码已确认，但接口返回跳转地址而非 token（需要 ticket 换 token）：%s", truncate(jump, 160))
		}
		msg := respError(payload)
		if msg == "" || strings.Contains(msg, "QrCodeRollLoginFail") || strings.Contains(msg, "未扫") {
			return "", 0, "", false, nil // 继续等
		}
		return "", 0, "", false, fmt.Errorf("%s", msg)
	}
	status = "token"
	log.Printf("二维码已确认，拿到 e189AccessToken（%s…）", prefixOf(e189, 6))
	// 换 sessionKey
	u2 := apiURL + "/family/manage/loginFamilyMerge.action"
	q2 := clientSuffix()
	q2.Set("e189AccessToken", e189)
	req2, _ := http.NewRequest("GET", u2+"?"+q2.Encode(), nil)
	for k, vs := range appKeySignature(u2, "GET") {
		for _, v := range vs {
			req2.Header.Set(k, v)
		}
	}
	req2.Header.Set("User-Agent", userAgent)
	req2.Header.Set("Accept", acceptHeader)
	resp2, err := httpClient.Do(req2)
	if err != nil {
		return "", 0, "", false, err
	}
	defer resp2.Body.Close()
	body2, _ := readAll(resp2.Body)
	log.Printf("换 sessionKey 响应: %s", truncate(maskSecret(strings.TrimSpace(string(body2))), 400))
	m2, err := parseBody(body2)
	if err != nil {
		return "", 0, "", false, err
	}
	sessionKey := firstOf(m2, "sessionKey", "session_key")
	if sessionKey == "" {
		return "", 0, "", false, fmt.Errorf("换取 sessionKey 失败: %s", respError(m2))
	}
	token, expires, err = tokenBySessionKey(sessionKey)
	if err != nil {
		return "", 0, "", false, err
	}
	return token, expires, sessionKey, true, nil
}

func tokenBySessionKey(sessionKey string) (string, int64, error) {
	sessionKey = strings.TrimSpace(sessionKey)
	if sessionKey == "" {
		return "", 0, fmt.Errorf("sessionKey 为空")
	}
	u := apiURL + "/open/oauth2/getAccessTokenBySsKey.action"
	q, headers := signNamed(url.Values{}, "sessionKey", sessionKey)
	req, err := http.NewRequest("GET", u+"?"+q, nil)
	if err != nil {
		return "", 0, err
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", acceptHeader)
	req.Header.Set("Content-Type", "application/json")
	for k, vs := range headers {
		for _, v := range vs {
			req.Header.Set(k, v)
		}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	body, _ := readAll(resp.Body)
	m, err := parseBody(body)
	if err != nil {
		return "", 0, err
	}
	token := firstOf(m, "accessToken", "access_token", "token")
	if token == "" {
		return "", 0, fmt.Errorf("换取 accessToken 失败: %s", respError(m))
	}
	// expiresIn 的口径在不同接口上不一样（秒 / 毫秒 / 绝对时间都见过），
	// 留一行原始响应，续期时间算歪时能直接看出是哪种。
	log.Printf("换 token 响应: %s", truncate(maskSecret(strings.TrimSpace(string(body))), 300))
	expires := normalizeExpiry(firstInt(m, "expiresIn", "expires_in"))
	return token, expires, nil
}

// normalizeExpiry 把 expiresIn 的三种口径统一成毫秒时间戳。
func normalizeExpiry(v int64) int64 {
	switch {
	case v <= 0:
		return 0
	case v > 1e11:
		return v
	case v > 1e9:
		return v * 1000
	default:
		return nowMS() + v*1000
	}
}

func parseSessionKey(raw string) (string, error) {
	text := strings.TrimSpace(raw)
	text = strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(text, "Cookie:"), "cookie:"))
	if text == "" {
		return "", fmt.Errorf("请粘贴 COOKIE_LOGIN_USER 或 SESSION_KEY")
	}
	value := text
	for _, part := range strings.Split(text, ";") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 && strings.EqualFold(kv[0], "COOKIE_LOGIN_USER") {
			value = kv[1]
		}
	}
	if unescaped, err := url.QueryUnescape(value); err == nil {
		value = unescaped
	}
	if i := strings.Index(value, "SESSION_KEY="); i >= 0 {
		rest := value[i+len("SESSION_KEY="):]
		if j := strings.IndexAny(rest, "&; \t\"'"); j >= 0 {
			rest = rest[:j]
		}
		return strings.TrimSpace(rest), nil
	}
	plain := strings.Trim(value, "'\" ")
	if len(plain) >= 28 {
		return plain, nil
	}
	return "", fmt.Errorf("没能从里面找到 SESSION_KEY")
}

func readAll(r io.Reader) ([]byte, error) {
	return io.ReadAll(io.LimitReader(r, 16<<20))
}

// ---------------------------------------------------------------- 目录 / 直链

type Node struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	IsDir    bool   `json:"is_dir"`
	Size     int64  `json:"size"`
	FamilyID string `json:"family_id"`
}

func listFamilies(c *CloudClient) ([]map[string]any, error) {
	m, err := c.get("/open/family/manage/getFamilyList.action", url.Values{})
	if err != nil {
		return nil, err
	}
	items, _ := m["familyInfoResp"].([]any)
	out := []map[string]any{}
	for _, it := range items {
		d, _ := it.(map[string]any)
		fid := numStr(d, "familyId")
		if fid == "" {
			continue
		}
		out = append(out, map[string]any{
			"family_id": fid, "name": firstOf(d, "remarkName", "familyName"),
		})
	}
	return out, nil
}

func listFiles(c *CloudClient, folderID, familyID string) ([]Node, error) {
	if folderID == "" {
		folderID = rootFolderID
	}
	out := []Node{}
	for page := 1; page <= 200; page++ {
		p := url.Values{}
		p.Set("pageNum", fmt.Sprint(page))
		p.Set("pageSize", "200")
		p.Set("mediaType", "0")
		p.Set("iconOption", "5")
		p.Set("orderBy", "lastOpTime")
		p.Set("descending", "true")
		api := "/open/file/listFiles.action"
		if familyID != "" {
			p.Set("familyId", familyID)
			if folderID != rootFolderID {
				p.Set("folderId", folderID)
			}
			api = "/open/family/file/listFiles.action"
		} else {
			p.Set("folderId", folderID)
		}
		m, err := c.get(api, p)
		if err != nil {
			return out, err
		}
		ao, _ := m["fileListAO"].(map[string]any)
		if ao == nil {
			break
		}
		folders, _ := ao["folderList"].([]any)
		files, _ := ao["fileList"].([]any)
		for _, it := range folders {
			d, _ := it.(map[string]any)
			out = append(out, Node{ID: firstOf(d, "id"), Name: firstOf(d, "name"), IsDir: true, FamilyID: familyID})
		}
		for _, it := range files {
			d, _ := it.(map[string]any)
			out = append(out, Node{ID: firstOf(d, "id"), Name: firstOf(d, "name"),
				Size: firstInt(d, "size"), FamilyID: familyID})
		}
		total := firstInt(ao, "count")
		if int64(len(folders)+len(files)) == 0 || int64(page*200) >= total {
			break
		}
	}
	return out, nil
}

// 取直链时用的浏览器 UA。天翼下发的下载地址会先 302 一跳，
// 这一跳对 UA 敏感，用 TV 端的 UA 有时会被拦。
const browserUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
	"(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"

// followClient 只跟一跳：不自动重定向，自己读 Location。
var followClient = &http.Client{
	Timeout: 20 * time.Second,
	CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	},
	Transport: httpClient.Transport,
}

// resolveFinalLink 把「下载入口」跟一跳 302，换成 CDN 的真实地址。
//
// 参考实现就是这么做的：直接把入口地址丢给播放器，播放器每次 Range 请求都要先吃一跳
// 302，一集剧几十次请求就多几十次往返；换成最终地址后 302 只有一次，之后全是直连。
// 跟不到（没跳转 / 出错）就原样返回，不影响播放。
func resolveFinalLink(raw string) string {
	if raw == "" {
		return raw
	}
	req, err := http.NewRequest(http.MethodGet, raw, nil)
	if err != nil {
		return raw
	}
	req.Header.Set("User-Agent", browserUA)
	resp, err := followClient.Do(req)
	if err != nil {
		return raw
	}
	defer resp.Body.Close()
	// 只要响应头，body 读一点点就够（有些 CDN 不回 Content-Length，别整个读）
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		if loc := resp.Header.Get("Location"); loc != "" {
			return loc
		}
	}
	return raw
}

func downloadURL(c *CloudClient, fileID, familyID string) (string, error) {
	p := url.Values{}
	p.Set("fileId", fileID)
	api := "/open/file/getFileDownloadUrl.action"
	if familyID != "" {
		api = "/open/family/file/getFileDownloadUrl.action"
		p.Set("familyId", familyID)
	} else {
		p.Set("dt", "1")
	}
	m, err := c.get(api, p)
	if err != nil {
		return "", err
	}
	u := firstOf(m, "fileDownloadUrl")
	if u == "" {
		return "", fmt.Errorf("云盘没有返回下载地址")
	}
	u = html.UnescapeString(strings.ReplaceAll(u, "&amp;", "&"))
	return resolveFinalLink(u), nil
}
