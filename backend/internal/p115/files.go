package p115

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"
)

// Node 是 115 上的一个文件或目录。
type Node struct {
	ID       string
	ParentID string
	Name     string
	Pickcode string
	Sha1     string
	Size     int64
	IsDir    bool
	Ctime    int64
	Mtime    int64
}

// normalizeNode 把 115 的字段（不同接口键名不一致）整理成 Node。
func normalizeNode(m map[string]any) Node {
	// 有些接口把真正的数据再包一层
	for _, wrapper := range []string{"data", "file", "info"} {
		if inner, ok := m[wrapper].(map[string]any); ok {
			m = inner
			break
		}
	}
	n := Node{
		Name:     str(m, "n", "name", "file_name", "fn"),
		Pickcode: str(m, "pc", "pickcode", "pick_code"),
		Sha1:     strings.ToUpper(str(m, "sha", "sha1", "file_sha1")),
		Size:     num(m, "s", "size", "file_size", "fs"),
		Ctime:    num(m, "tp", "ctime", "create_time"),
		Mtime:    num(m, "te", "mtime", "utime", "update_time"),
	}
	fid := str(m, "fid", "file_id")
	cid := str(m, "cid", "category_id")
	isDir, ok := boolv(m, "is_dir", "is_folder", "folder")
	if !ok {
		if fc, okc := m["fc"]; okc {
			// fc=0 是目录，1 是文件
			isDir = num(map[string]any{"fc": fc}, "fc") == 0
		} else if n.Sha1 != "" {
			isDir = false
		} else if fid == "" {
			isDir = true
		}
	}
	n.IsDir = isDir
	if isDir {
		n.ID = cid
		n.ParentID = str(m, "pid", "parent_id")
	} else {
		n.ID = fid
		n.ParentID = cid
	}
	return n
}

// ListDir 列一个目录的直接子项（自动翻页）。cid=0 是根目录。
func (c *Client) ListDir(ctx context.Context, cid string) ([]Node, error) {
	if cid == "" {
		cid = "0"
	}
	out := make([]Node, 0, 128)
	offset := 0
	const pageSize = 200
	for page := 0; page < 2000; page++ {
		q := url.Values{}
		q.Set("aid", "1")
		q.Set("cid", cid)
		q.Set("o", "user_ptime")
		q.Set("asc", "0")
		q.Set("offset", fmt.Sprint(offset))
		q.Set("show_dir", "1")
		q.Set("limit", fmt.Sprint(pageSize))
		q.Set("snap", "0")
		q.Set("natsort", "1")
		q.Set("record_open_time", "1")
		q.Set("format", "json")
		q.Set("fc_mix", "0")
		m, err := c.getJSON(ctx, WebAPI+pathFiles+"?"+q.Encode(), nil)
		if err != nil {
			return nil, err
		}
		if err := check("列目录", m); err != nil {
			return nil, err
		}
		var items []map[string]any
		if arr, ok := m["data"].([]any); ok {
			items = make([]map[string]any, 0, len(arr))
			for _, it := range arr {
				if d, ok := it.(map[string]any); ok {
					items = append(items, d)
				}
			}
		}
		for _, it := range items {
			n := normalizeNode(it)
			if n.ID == "" || n.ID == "0" {
				continue
			}
			// 顺手记进目录树：事件监控只拿到 parent_id，要靠这棵树拼出路径
			if n.IsDir {
				c.NoteDir(n.ID, n.ParentID, n.Name)
			}
			out = append(out, n)
		}
		total := int(num(m, "count"))
		offset += len(items)
		if len(items) == 0 || (total > 0 && offset >= total) || len(items) < pageSize {
			break
		}
		time.Sleep(150 * time.Millisecond)
	}
	return out, nil
}

// Walk 深度优先遍历目录。cb 返回非 nil 会中断遍历。
func (c *Client) Walk(ctx context.Context, cid string, depth int, cb func(n Node, rel string) error) error {
	if depth > 64 {
		return nil
	}
	nodes, err := c.ListDir(ctx, cid)
	if err != nil {
		return err
	}
	for _, n := range nodes {
		if err := cb(n, ""); err != nil {
			return err
		}
		if n.IsDir {
			if err := c.Walk(ctx, n.ID, depth+1, cb); err != nil {
				return err
			}
		}
	}
	return nil
}

// Mkdir 在 parentID 下创建目录，返回新目录 id。
func (c *Client) Mkdir(ctx context.Context, parentID, name string) (string, error) {
	if parentID == "" {
		parentID = "0"
	}
	form := url.Values{}
	form.Set("pid", parentID)
	form.Set("cname", name)
	m, err := c.postForm(ctx, WebAPI+pathFilesAdd, form)
	if err != nil {
		return "", err
	}
	if err := check("创建目录", m); err != nil {
		return "", err
	}
	cid := str(m, "cid", "file_id", "id")
	if cid == "" {
		cid = str(subMap(m, "data"), "cid", "file_id", "id")
	}
	if cid == "" {
		return "", &Error{Op: "创建目录", Msg: "响应里没有 cid"}
	}
	return cid, nil
}

// EnsureDir 按名字逐级创建目录，返回最后一级的 id。
// 已存在同名目录时直接复用。
func (c *Client) EnsureDir(ctx context.Context, parentID string, names ...string) (string, error) {
	cid := parentID
	if cid == "" {
		cid = "0"
	}
	for _, name := range names {
		name = strings.TrimSpace(name)
		if name == "" || name == "." || name == ".." {
			continue
		}
		nodes, err := c.ListDir(ctx, cid)
		if err != nil {
			return "", err
		}
		found := ""
		for _, n := range nodes {
			if n.IsDir && n.Name == name {
				found = n.ID
				break
			}
		}
		if found == "" {
			id, err := c.Mkdir(ctx, cid, name)
			if err != nil {
				return "", err
			}
			found = id
		}
		cid = found
	}
	return cid, nil
}

// EnsureDirPath 按 "a/b/c" 的相对路径逐级创建。
func (c *Client) EnsureDirPath(ctx context.Context, parentID, rel string) (string, error) {
	rel = strings.ReplaceAll(rel, "\\", "/")
	parts := strings.Split(strings.Trim(rel, "/"), "/")
	return c.EnsureDir(ctx, parentID, parts...)
}

// Delete 删除文件/目录（支持批量）。会进 115 回收站。
func (c *Client) Delete(ctx context.Context, ids []string) error {
	return c.DeleteIn(ctx, ids, "0")
}

// DeleteIn 在指定父目录下删除：115 的删除接口要带 pid，
// 传错父目录会回「服务器开小差了」。
func (c *Client) DeleteIn(ctx context.Context, ids []string, pid string) error {
	ids = cleanIDs(ids)
	if len(ids) == 0 {
		return nil
	}
	if pid == "" {
		pid = "0"
	}
	base := url.Values{}
	base.Set("pid", pid)
	m, err := postFormMulti(ctx, c, WebAPI+pathFilesDelete, base, "fid", ids)
	if err != nil {
		return err
	}
	return check("删除文件", m)
}

// Copy 把文件复制到 pid。返回 115 的原始响应。
func (c *Client) Copy(ctx context.Context, ids []string, pid string) error {
	ids = cleanIDs(ids)
	if len(ids) == 0 {
		return nil
	}
	base := url.Values{}
	base.Set("pid", pid)
	m, err := postFormMulti(ctx, c, WebAPI+pathFilesCopy, base, "fid", ids)
	if err != nil {
		return err
	}
	return check("复制文件", m)
}

// Move 移动文件到 pid。
func (c *Client) Move(ctx context.Context, ids []string, pid string) error {
	ids = cleanIDs(ids)
	if len(ids) == 0 {
		return nil
	}
	base := url.Values{}
	base.Set("pid", pid)
	m, err := postFormMulti(ctx, c, WebAPI+pathFilesMove, base, "fid", ids)
	if err != nil {
		return err
	}
	return check("移动文件", m)
}

func cleanIDs(ids []string) []string {
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id != "" && id != "0" {
			out = append(out, id)
		}
	}
	return out
}

// Details 按 pickcode 或 file_id 查单个节点。
func (c *Client) Details(ctx context.Context, key string) (*Node, error) {
	if key == "" {
		return nil, &Error{Op: "查询文件", Msg: "缺少 pickcode / file_id"}
	}
	for _, param := range []string{"pickcode", "file_id"} {
		q := url.Values{}
		q.Set(param, key)
		m, err := c.getJSON(ctx, WebAPI+pathFilesDetails+"?"+q.Encode(), nil)
		if err != nil {
			continue
		}
		if check("查询文件", m) != nil {
			continue
		}
		payload := firstMap(m["data"])
		if payload == nil {
			continue
		}
		n := normalizeNode(payload)
		if n.ID == "" {
			n.ID = key
		}
		n.Pickcode = firstNonEmpty(n.Pickcode, key)
		return &n, nil
	}
	return nil, &Error{Op: "查询文件", Msg: "没找到这个文件（可能已被删除或移动）"}
}

// firstMap 把 {"data":[{...}]} / {"data":{"list":[...]}} 统一成单个 map。
func firstMap(v any) map[string]any {
	for i := 0; i < 5; i++ {
		switch t := v.(type) {
		case []any:
			if len(t) == 0 {
				return nil
			}
			v = t[0]
		case map[string]any:
			moved := false
			for _, key := range []string{"list", "data", "file", "info"} {
				if inner, ok := t[key]; ok {
					if _, isMap := inner.(map[string]any); isMap {
						v = inner
						moved = true
						break
					}
					if arr, isArr := inner.([]any); isArr {
						if len(arr) == 0 {
							return nil
						}
						v = arr[0]
						moved = true
						break
					}
				}
			}
			if !moved {
				return t
			}
		default:
			return nil
		}
	}
	return nil
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// DownloadURL 取播放直链。
//
// 115 直链带着临时签名，且要求「取链接时的 UA」和「播 CDN 时的 UA」一致，
// 所以缓存键要带上 UA（调用方传入）。
//
// 顺序有讲究：**先走加密通道**。明文的 GET /files/download 只放行 200MB
// 以内的文件，视频基本都超过，会返回 50028「文件大小超出限制，请使用115
// 电脑端下载」——这正是电视端播放失败最常见的原因。加密通道没有这个限制。
func (c *Client) DownloadURL(ctx context.Context, pickcode, ua string, ttl time.Duration) (string, map[string]string, error) {
	return c.DownloadURLVia(ctx, pickcode, ua, "", ttl)
}

// DownloadURLVia 是指定通道的取直链。via 为空或 "auto" 时按
// android → chrome → web 顺序试；也可以指定单个通道，用来对比不同通道下发的链接。
//
// 为什么要安卓通道优先：安卓客户端取链接时官方就是不带 Cookie 的（只带 UA），
// 它下发的直链 CDN 只校验 UA，播放器 302 过去就能直接拉流；而 chrome 通道下发
// 的是 f=3，必须带上「取链接那次响应里的 cookie」，302 带不过去就只能由本机中转。
func (c *Client) DownloadURLVia(ctx context.Context, pickcode, ua, via string, ttl time.Duration) (string, map[string]string, error) {
	if ua == "" {
		ua = c.UA
	}
	key := "dl|" + pickcode + "|" + ua + "|" + via
	if u, h, ok := c.cacheGet(key); ok {
		return u, h, nil
	}
	if strings.TrimSpace(pickcode) == "" {
		return "", nil, &Error{Op: "获取直链", Msg: "缺少 pickcode（这条 strm 是旧版本生成的，请重新生成一次）"}
	}

	var reasons []string
	uaHeader := map[string]string{"User-Agent": ua}

	// 方式一：安卓客户端通道（payload 用 pick_code；鉴权仍靠账号 Cookie）
	if via == "" || via == "auto" || via == "android" {
		form := url.Values{}
		form.Set("data", encryptPayload([]byte(fmt.Sprintf(`{"pick_code":%q}`, pickcode))))
		if body, hdr, err := c.postFormRaw(ctx, ProAPI+pathAndroidDown, form, uaHeader); err == nil {
			if u, h := parseDownload(body, hdr); u != "" {
				c.cachePut(key, u, h, ttl)
				return u, h, nil
			}
			reasons = append(reasons, reasonOf(body))
		} else {
			reasons = append(reasons, err.Error())
		}
		if via == "android" {
			return "", nil, downloadError(reasons)
		}
	}

	payload := fmt.Sprintf(`{"pickcode":"%s"}`, pickcode)

	// 方式二：web 下载接口的加密通道（data=RSA 密文）
	if via == "" || via == "auto" || via == "web" {
		form := url.Values{}
		form.Set("data", encryptPayload([]byte(payload)))
		if body, hdr, err := c.postFormRaw(ctx, WebAPI+pathFilesDown, form, uaHeader); err == nil {
			if u, h := parseDownload(body, hdr); u != "" {
				c.cachePut(key, u, h, ttl)
				return u, h, nil
			}
			reasons = append(reasons, reasonOf(body))
		} else {
			reasons = append(reasons, err.Error())
		}
		if via == "web" {
			return "", nil, downloadError(reasons)
		}
	}

	// 方式三：115 电脑端（chrome）通道
	if via == "" || via == "auto" || via == "chrome" {
		appPayload := fmt.Sprintf(`{"pickcode":"%s","user_id":%q}`, pickcode, c.UID(ctx))
		form2 := url.Values{}
		form2.Set("data", encryptPayload([]byte(appPayload)))
		if body, hdr, err := c.postFormRaw(ctx, ProAPI+pathChromeDown, form2, uaHeader); err == nil {
			if u, h := parseDownload(body, hdr); u != "" {
				c.cachePut(key, u, h, ttl)
				return u, h, nil
			}
			reasons = append(reasons, reasonOf(body))
		} else {
			reasons = append(reasons, err.Error())
		}
		if via == "chrome" {
			return "", nil, downloadError(reasons)
		}
	}

	// 方式四：明文 GET（小文件、或加密通道临时不可用时兜底）
	q := url.Values{}
	q.Set("pickcode", pickcode)
	if m, err := c.getJSON(ctx, WebAPI+pathFilesDown+"?"+q.Encode(), uaHeader); err == nil {
		if u := str(m, "file_url", "url"); u != "" {
			c.cachePut(key, u, nil, ttl)
			return u, nil, nil
		}
		reasons = append(reasons, reasonOfMap(m))
	}

	return "", nil, downloadError(reasons)
}

func downloadError(reasons []string) error {
	msg := "115 没有返回下载地址"
	if len(reasons) > 0 {
		msg += "（" + strings.Join(dedupe(reasons), "；") + "）"
	}
	return &Error{Op: "获取直链", Msg: msg}
}

// parseDownload 解析下载接口的响应：state 为真时 data 是密文，解出来再找链接。
func parseDownload(body []byte, hdr http.Header) (string, map[string]string) {
	m, err := parseJSON(body)
	if err != nil {
		return "", nil
	}
	if data, ok := m["data"].(string); ok && data != "" {
		if dm, err := decryptJSON(data); err == nil {
			if u, h := findURL(dm, 0); u != "" {
				return u, mergeHeader(h, m, hdr)
			}
		}
	}
	if u, h := findURL(m, 0); u != "" {
		return u, mergeHeader(h, m, hdr)
	}
	return "", nil
}

// mergeHeader 把播放时需要的头补齐。
//
// 115 的直链带 f=3 时，CDN 会校验 User-Agent 和「取链接那次」下发的 cookie，
// 那把 cookie 在响应的 Set-Cookie 里。
func mergeHeader(h map[string]string, m map[string]any, hdr http.Header) map[string]string {
	out := map[string]string{}
	for k, v := range h {
		out[k] = v
	}
	if hdr != nil {
		for _, c := range hdr.Values("Set-Cookie") {
			if i := strings.IndexByte(c, ';'); i > 0 {
				c = c[:i]
			}
			if k, _, ok := strings.Cut(c, "="); ok && k != "" {
				if out["Cookie"] != "" {
					out["Cookie"] += "; " + c
				} else {
					out["Cookie"] = c
				}
			}
		}
	}
	if v := str(m, "user_agent"); v != "" {
		out["User-Agent"] = v
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func reasonOf(body []byte) string {
	m, err := parseJSON(body)
	if err != nil {
		return truncate(strings.TrimSpace(string(body)), 120)
	}
	return reasonOfMap(m)
}

func reasonOfMap(m map[string]any) string {
	msg := str(m, "msg", "error", "message")
	code := num(m, "msg_code", "errno", "code")
	switch {
	case msg != "" && code != 0:
		return fmt.Sprintf("%d %s", code, msg)
	case msg != "":
		return msg
	case code != 0:
		return fmt.Sprintf("错误码 %d", code)
	}
	return "响应里没有下载地址"
}

func dedupe(in []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(in))
	for _, s := range in {
		if s == "" || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

// findURL 在嵌套响应里找第一个 http 链接。
func findURL(v any, depth int) (string, map[string]string) {
	if depth > 6 || v == nil {
		return "", nil
	}
	switch t := v.(type) {
	case string:
		if strings.HasPrefix(t, "http") {
			return t, nil
		}
	case map[string]any:
		for _, key := range []string{"url", "download_url", "downurl", "file_url"} {
			if s, ok := t[key].(string); ok && strings.HasPrefix(s, "http") {
				var h map[string]string
				if hh, ok := t["headers"].(map[string]any); ok {
					h = map[string]string{}
					for k, val := range hh {
						if s, ok := val.(string); ok {
							h[k] = s
						}
					}
				}
				return s, h
			}
		}
		for _, val := range t {
			if u, h := findURL(val, depth+1); u != "" {
				return u, h
			}
		}
	case []any:
		for _, val := range t {
			if u, h := findURL(val, depth+1); u != "" {
				return u, h
			}
		}
	}
	return "", nil
}

// SplitPath 把 "电影/2024/片名.mkv" 拆成目录层级。
func SplitPath(rel string) ([]string, string) {
	rel = strings.ReplaceAll(rel, "\\", "/")
	dir, file := path.Split(rel)
	dir = strings.Trim(dir, "/")
	if dir == "" {
		return nil, file
	}
	return strings.Split(dir, "/"), file
}
