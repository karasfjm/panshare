package p115

import (
	"context"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"
)

// 115 的「生活事件」：网盘上真实发生过的增删改动作，按时间倒序给出。
// 事件监控就靠它——不用轮询整棵目录树，来一个事件处理一个文件。
//
// 类型码（官方枚举，参考 p115client / p115strmhelper）：
//
//	1  上传图片        2  上传文件或目录   5  移动图片       6  移动文件或目录
//	14 接收文件        17 新增目录         18 复制目录      20 目录改名
//	22 删除文件或目录  23 复制文件         24 文件改名
const (
	LifeUploadImage = 1
	LifeUploadFile  = 2
	LifeMoveImage   = 5
	LifeMoveFile    = 6
	LifeReceive     = 14
	LifeNewFolder   = 17
	LifeCopyFolder  = 18
	LifeRenameDir   = 20
	LifeDelete      = 22
	LifeCopyFile    = 23
	LifeRenameFile  = 24
)

// LifeEvent 是一条操作事件。
type LifeEvent struct {
	ID         int64
	Type       int
	FileID     string
	ParentID   string
	ParentName string // 事件自带的父目录名（只有一层，用来兜底拼路径）
	Name       string
	Pickcode   string
	Sha1       string
	Size       int64
	IsDir      bool
	Time       int64 // 事件发生时间（秒）
}

// IsCreate 表示「多了个东西」。复制与接收也算新增。
func (e LifeEvent) IsCreate() bool {
	switch e.Type {
	case LifeUploadImage, LifeUploadFile, LifeReceive, LifeCopyFolder, LifeCopyFile:
		return true
	}
	return false
}

// IsFolderEvent 表示目录层面的变化：子项不一定有各自的事件，
// 遇到这种要退化成「把该目录增量扫一遍」。
func (e LifeEvent) IsFolderEvent() bool {
	switch e.Type {
	case LifeNewFolder, LifeCopyFolder, LifeRenameDir:
		return true
	}
	return false
}

// LifeEvents 拉一段生活事件：从 (fromTime, fromID) 之后开始，按时间正序返回。
//
// fromTime 是上一轮最后一条事件的 update_time，fromID 是它的 id。
// 两者一起当游标，避免同一秒内的多条事件翻页时错位。
// limit 是本次最多返回多少条（服务端有可能再多一点点）。
func (c *Client) LifeEvents(ctx context.Context, fromTime, fromID int64, limit int) ([]LifeEvent, error) {
	if limit <= 0 {
		limit = 200
	}
	const pageSize = 100
	const maxPages = 40 // 单轮最多翻 4000 条，够追上几天的量

	out := make([]LifeEvent, 0, limit)
	offset := 0
	for page := 0; page < maxPages && len(out) < limit; page++ {
		q := url.Values{}
		q.Set("limit", strconv.Itoa(pageSize))
		q.Set("offset", strconv.Itoa(offset))
		m, err := c.getJSON(ctx, WebAPI+pathBehaviorDetail+"?"+q.Encode(), nil)
		if err != nil {
			return out, err
		}
		if err := check("拉取115事件", m); err != nil {
			return out, err
		}
		list := lifeList(m)
		if len(list) == 0 {
			break
		}
		reachedOld := false
		for _, item := range list {
			e := parseLifeEvent(item)
			if e.ID == 0 && e.FileID == "" {
				continue
			}
			// 比游标还老的就到底了；服务端按时间倒序，可以直接停
			if fromID > 0 && e.ID > 0 && e.ID <= fromID {
				reachedOld = true
				break
			}
			if fromTime > 0 && e.Time > 0 && e.Time < fromTime {
				reachedOld = true
				break
			}
			out = append(out, e)
			if len(out) >= limit {
				break
			}
		}
		next, hasNext := lifeNextPage(m, len(list))
		if reachedOld || !hasNext || next <= offset {
			break
		}
		offset = next
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Time != out[j].Time {
			return out[i].Time < out[j].Time
		}
		return out[i].ID < out[j].ID
	})
	return out, nil
}

// lifeList 从响应里取事件数组，兼容 data.list / data.items 几种包法。
func lifeList(m map[string]any) []map[string]any {
	holders := []map[string]any{m}
	if sub := subMap(m, "data"); len(sub) > 0 {
		holders = append([]map[string]any{sub}, holders...)
	}
	for _, holder := range holders {
		for _, key := range []string{"list", "items", "data"} {
			if list := listOf(holder, key); len(list) > 0 {
				return list
			}
		}
	}
	return nil
}

// lifeNextPage 判断还有没有下一页，并算出下一个 offset。
func lifeNextPage(m map[string]any, got int) (int, bool) {
	data := subMap(m, "data")
	if data == nil {
		data = m
	}
	if v, ok := boolv(data, "next_page", "has_next", "next"); ok {
		if !v {
			return 0, false
		}
	}
	if n := num(data, "count", "total"); n > 0 && n <= int64(got) {
		// count 是总数，这一页就拉完了
		return 0, false
	}
	if off, ok := data["offset"]; ok {
		if base, err := strconv.Atoi(strings.TrimSpace(toString(off))); err == nil {
			return base + got, true
		}
	}
	return got, got > 0 && got >= 100
}

func parseLifeEvent(m map[string]any) LifeEvent {
	e := LifeEvent{
		ID:         num(m, "id", "event_id"),
		Type:       int(num(m, "type", "behavior_type")),
		FileID:     str(m, "file_id", "fid"),
		ParentID:   str(m, "parent_id", "pid", "cid"),
		ParentName: str(m, "parent_name"),
		Name:       str(m, "file_name", "n", "name"),
		Pickcode:   str(m, "pick_code", "pickcode", "pc"),
		Sha1:       strings.ToUpper(str(m, "sha1", "file_sha1", "sha")),
		Size:       num(m, "file_size", "size", "s"),
		Time:       num(m, "update_time", "create_time", "utime", "te", "tp"),
	}
	// file_category：0 = 目录，1 = 文件
	if v, ok := m["file_category"]; ok {
		e.IsDir = num(map[string]any{"c": v}, "c") == 0
	}
	if v, ok := boolv(m, "is_dir", "is_folder"); ok {
		e.IsDir = v
	}
	// 目录事件的「文件 id」在部分接口里是 cid
	if e.FileID == "" {
		e.FileID = str(m, "cid", "category_id")
	}
	if e.IsDir && e.FileID == "" {
		e.IsDir = false
	}
	return e
}

func toString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case int64:
		return strconv.FormatInt(t, 10)
	case int:
		return strconv.Itoa(t)
	case bool:
		if t {
			return "1"
		}
		return "0"
	}
	return ""
}

// ---------------------------------------------------------------- 目录树缓存

// dirEntry 是目录树里的一节：名字 + 父目录 cid。
// 115 没有可靠的「按 cid 查绝对路径」接口（files/getpath、files/details
// 对目录都直接报服务器开小差了），只能靠列目录时把树记下来，事件来了自己拼。
type dirEntry struct {
	name string
	pid  string
}

// NoteDir 记一个目录节点（ListDir 内部已自动调用）。
func (c *Client) NoteDir(id, parentID, name string) {
	if id == "" || id == "0" || name == "" {
		return
	}
	c.pathMu.Lock()
	defer c.pathMu.Unlock()
	if c.dirCache == nil {
		c.dirCache = map[string]dirEntry{}
	}
	if len(c.dirCache) > 200000 {
		c.dirCache = map[string]dirEntry{}
	}
	c.dirCache[id] = dirEntry{name: name, pid: parentID}
}

// SetDirPath 直接把「这个 cid 就是这条绝对路径」写进缓存。
// 配置里的监控目录是已知路径，先登记，事件一到就能立刻对上。
func (c *Client) SetDirPath(cid, absPath string) {
	cid = strings.TrimSpace(cid)
	if cid == "" || cid == "0" {
		return
	}
	p := cleanAbs(absPath)
	if p == "" {
		return
	}
	c.pathMu.Lock()
	if c.pathCache == nil {
		c.pathCache = map[string]string{}
	}
	c.pathCache[cid] = p
	c.pathMu.Unlock()
}

func cleanAbs(p string) string {
	p = strings.ReplaceAll(strings.TrimSpace(p), "\\", "/")
	if p == "" || p == "/" {
		return ""
	}
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	for strings.Contains(p, "//") {
		p = strings.ReplaceAll(p, "//", "/")
	}
	return strings.TrimSuffix(p, "/")
}

// DirPath 由目录 cid 反查网盘绝对路径（/A/B）：先查已知路径，
// 再顺目录树往上拼，最后才退回 files/details（对目录常常查不到）。
func (c *Client) DirPath(ctx context.Context, cid string) (string, error) {
	cid = strings.TrimSpace(cid)
	if cid == "" || cid == "0" {
		return "", nil
	}
	if p, ok := c.pathGet(cid); ok {
		return p, nil
	}
	var segs []string
	cur := cid
	seen := map[string]bool{}
	var lastErr error
	broken := false
	for len(segs) < 64 && cur != "" && cur != "0" {
		if seen[cur] {
			break
		}
		seen[cur] = true
		if p, ok := c.pathGet(cur); ok {
			for _, s := range strings.Split(strings.Trim(p, "/"), "/") {
				if s != "" {
					segs = append(segs, s)
				}
			}
			cur = "0"
			break
		}
		e, ok := c.dirGet(cur)
		if !ok {
			n, err := c.Details(ctx, cur)
			if err != nil || n.Name == "" || n.IsDir && n.ParentID == "" {
				lastErr = err
				broken = true
				break
			}
			e = dirEntry{name: n.Name, pid: n.ParentID}
			c.NoteDir(cur, n.ParentID, n.Name)
		}
		segs = append(segs, e.name)
		cur = e.pid
	}
	if len(segs) == 0 {
		if lastErr != nil {
			return "", lastErr
		}
		return "", nil
	}
	for i, j := 0, len(segs)-1; i < j; i, j = i+1, j-1 {
		segs[i], segs[j] = segs[j], segs[i]
	}
	full := "/" + strings.Join(segs, "/")
	if broken {
		// 拼到一半断了：报错让调用方跳过这个事件，别拿半截路径去匹配
		return full, fmt.Errorf("目录路径拼不全: %s", full)
	}
	return full, nil
}

func (c *Client) dirGet(cid string) (dirEntry, bool) {
	c.pathMu.Lock()
	defer c.pathMu.Unlock()
	e, ok := c.dirCache[cid]
	return e, ok
}

func (c *Client) pathGet(cid string) (string, bool) {
	c.pathMu.Lock()
	defer c.pathMu.Unlock()
	v, ok := c.pathCache[cid]
	return v, ok
}

func (c *Client) pathPut(cid, p string) {
	c.pathMu.Lock()
	defer c.pathMu.Unlock()
	if c.pathCache == nil {
		c.pathCache = map[string]string{}
	}
	if len(c.pathCache) > 200000 {
		c.pathCache = map[string]string{}
	}
	c.pathCache[cid] = p
}

// ForgetPaths 在收到改名/移动事件后清掉路径缓存，避免拼出旧路径。
func (c *Client) ForgetPaths() {
	c.pathMu.Lock()
	c.pathCache = map[string]string{}
	c.dirCache = map[string]dirEntry{}
	c.pathMu.Unlock()
}
