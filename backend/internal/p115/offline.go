package p115

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"panshare/internal/p115/m115"
)

// OfflineTask 是 115 离线下载里的一个任务。
type OfflineTask struct {
	InfoHash  string
	Name      string
	Size      int64
	Status    string // missing / running / done / failed
	Percent   int
	StatusTxt string
}

var ed2kRe = regexp.MustCompile(`(?i)^ed2k://\|file\|([^|]*)\|(\d+)\|([0-9a-f]{32})\|`)

// ParseEd2k 解析 ed2k 链接，取出文件名、大小、hash。
func ParseEd2k(link string) (name string, size int64, hash string, err error) {
	m := ed2kRe.FindStringSubmatch(strings.TrimSpace(link))
	if len(m) < 4 {
		return "", 0, "", fmt.Errorf("不是有效的 ed2k 链接（需要 ed2k://|file|名字|大小|hash|/）")
	}
	size, _ = strconv.ParseInt(m[2], 10, 64)
	return m[1], size, strings.ToLower(m[3]), nil
}

// cloudDownCall 走 app 通道提交一个云下载请求。
//
// 与分享直链同一套包装：body 只有一个 data 字段（m115：随机密钥 + XOR + RSA），
// 且 UA 必须是 115 客户端那一串 —— 用浏览器 UA 会被判成网页版第三方请求。
// ac / app_ver 也要塞进明文里，客户端就是这么发的。
func (c *Client) cloudDownCall(ctx context.Context, api, action string, payload map[string]string) (map[string]any, error) {
	if payload == nil {
		payload = map[string]string{}
	}
	payload["ac"] = action
	payload["app_ver"] = CloudDownAppVer
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	key := m115.GenerateKey()
	form := url.Values{}
	form.Set("data", m115.Encode(raw, key))
	data, _, err := c.do(ctx, api, requestOptions{
		method:    http.MethodPost,
		form:      form,
		ua:        CloudDownUA,
		noReferer: true,
		extra:     map[string]string{"Origin": "https://115.com", "Accept": "application/json;charset=UTF-8"},
	})
	if err != nil {
		return nil, err
	}
	m, err := parseJSON(data)
	if err != nil {
		return nil, err
	}
	// 少数返回把真正的结果再包一层 m115：{"state":true,"data":"<base64>"}
	if s, ok := m["data"].(string); ok && s != "" {
		if dec, err2 := m115.Decode(s, key); err2 == nil {
			var sub map[string]any
			if json.Unmarshal(dec, &sub) == nil {
				m["data"] = sub
			}
		}
	}
	return m, nil
}

// checkCloud 校验云下载接口的返回：errcode != 0 或 state=false 都算失败。
func checkCloud(op string, m map[string]any) error {
	code := num(m, "errcode", "errno", "error_code", "code")
	if code != 0 {
		return &Error{Op: op, Code: int(code), Msg: str(m, "error_msg", "error", "message", "msg", "errmsg")}
	}
	return check(op, m)
}

// errTaskExists 是「离线任务已存在」（同一条链接重复提交）。
const errTaskExists = 10008

// OfflineAdd 提交离线任务（ed2k → 115 云端）。
func (c *Client) OfflineAdd(ctx context.Context, ed2kURL, cid string) error {
	if cid == "" {
		cid = "0"
	}
	submit := func() error {
		m, err := c.cloudDownCall(ctx, URLCloudDownAdd, "add_task_url", map[string]string{
			"url":        ed2kURL,
			"wp_path_id": cid,
		})
		if err != nil {
			return err
		}
		return checkCloud("提交离线任务", m)
	}

	err := submit()
	if err == nil {
		return nil
	}
	// 任务记录已存在：删掉旧记录再提交一次。
	// 任务记录不等于文件，删记录不会动已经转存好的文件，
	// 而「清理后又想再看」正是需要重新提交的场景。
	if e, ok := err.(*Error); ok && e.Code == errTaskExists {
		if _, _, hash, e2 := ParseEd2k(ed2kURL); e2 == nil && hash != "" {
			_ = c.OfflineDel(ctx, hash)
			time.Sleep(1200 * time.Millisecond)
			return submit()
		}
	}
	return err
}

// OfflineTaskInfo 查单条离线任务。
//
// 任务不存在时返回 (nil, nil)。注意单查与列表的 status 数字含义不一致
// （同一个已完成的 ed2k，列表里是 2、单查是 1），所以这里只认文字状态。
func (c *Client) OfflineTaskInfo(ctx context.Context, infoHash string) (*OfflineTask, error) {
	if infoHash == "" {
		return nil, nil
	}
	q := url.Values{}
	q.Set("ac", "get_user_task")
	q.Set("info_hash", infoHash)
	m, err := c.getJSON(ctx, URLCloudDownWeb+"?"+q.Encode(), map[string]string{"User-Agent": CloudDownUA})
	if err != nil {
		return nil, err
	}
	if err := checkCloud("查询离线任务", m); err != nil {
		return nil, err
	}
	item := m
	if sub, ok := m["data"].(map[string]any); ok && len(sub) > 0 {
		item = sub
	}
	if len(item) == 0 || (str(item, "info_hash", "hash") == "" && str(item, "name") == "") {
		return nil, nil
	}
	t := normalizeOffline(item, true)
	if t.InfoHash == "" {
		t.InfoHash = strings.ToLower(infoHash)
	}
	return &t, nil
}

// OfflineList 列出离线任务。
func (c *Client) OfflineList(ctx context.Context) ([]OfflineTask, error) {
	q := url.Values{}
	q.Set("ac", "task_lists")
	q.Set("page", "1")
	q.Set("page_size", "100")
	q.Set("stat", "0")
	m, err := c.getJSON(ctx, URLCloudDownWeb+"?"+q.Encode(), map[string]string{"User-Agent": CloudDownUA})
	if err != nil {
		return nil, err
	}
	if err := checkCloud("读取离线任务", m); err != nil {
		return nil, err
	}
	items := listOf(m, "tasks", "list")
	if len(items) == 0 {
		// {"data":{"list":[...]}} / {"data":[...]}
		items = listOf(m, "data")
		if len(items) == 0 {
			if sub, ok := m["data"].(map[string]any); ok {
				items = listOf(sub, "list", "tasks", "data")
			}
		}
	}
	out := make([]OfflineTask, 0, len(items))
	for _, it := range items {
		out = append(out, normalizeOffline(it, false))
	}
	return out, nil
}

// normalizeOffline 把一条离线任务统一成 OfflineTask。
//
// 文字状态优先：两套接口的 status 数字含义互相矛盾（列表 2=完成、单查 1=完成），
// 只有拿不到文字时才退回数字，且只在列表语境（single=false）下用。
func normalizeOffline(it map[string]any, single bool) OfflineTask {
	t := OfflineTask{
		InfoHash: strings.ToLower(str(it, "info_hash", "hash")),
		Name:     str(it, "name", "file_name", "title"),
		Size:     num(it, "size", "file_size"),
		Percent:  int(num(it, "percentDone", "percent", "display_percent")),
	}
	txt := str(it, "status_text", "statusText", "display_status")
	display := strings.ToLower(str(it, "display_status"))

	switch {
	case display == "finished" || display == "done" || display == "success",
		strings.Contains(txt, "成功"), strings.Contains(txt, "已完成"), strings.Contains(txt, "已下载"):
		t.Status, t.Percent = "done", 100
		return t
	case display == "failed" || display == "error" || display == "fail",
		strings.Contains(txt, "失败"), strings.Contains(txt, "错误"), strings.Contains(txt, "删除"):
		t.Status = "failed"
		t.StatusTxt = txt
		return t
	case txt != "":
		t.Status = "running"
		t.StatusTxt = txt
		return t
	}

	// 没有文字状态：只有列表接口的数字可以信。
	if !single {
		if code, ok := it["status"].(float64); ok {
			switch int(code) {
			case 2:
				t.Status, t.Percent = "done", 100
				return t
			case 3:
				t.Status = "failed"
				return t
			}
		}
	}
	if t.Percent >= 100 && (str(it, "info_hash", "hash") != "" || t.Name != "") {
		t.Status, t.Percent = "done", 100
		return t
	}
	t.Status = "running"
	return t
}

// OfflineDel 删除离线任务记录（不会删除已经转存好的文件）。
func (c *Client) OfflineDel(ctx context.Context, infoHash string) error {
	if infoHash == "" {
		return nil
	}
	form := url.Values{}
	form.Set("hash[0]", infoHash)
	m, err := c.postFormUA(ctx, URLCloudDownWeb+"?ac=task_del", form, CloudDownUA)
	if err != nil {
		return err
	}
	return checkCloud("删除离线任务", m)
}

// postFormUA 用指定 UA 发一个普通表单 POST（云下载的 /web/ 通道用）。
func (c *Client) postFormUA(ctx context.Context, rawURL string, form url.Values, ua string) (map[string]any, error) {
	data, _, err := c.do(ctx, rawURL, requestOptions{
		method:    http.MethodPost,
		form:      form,
		ua:        ua,
		noReferer: true,
		extra:     map[string]string{"Origin": "https://115.com"},
	})
	if err != nil {
		return nil, err
	}
	return parseJSON(data)
}

// FindByName 在目录里按名字找条目（离线任务完成后定位文件用）。
func (c *Client) FindByName(ctx context.Context, cid, name string) (*Node, error) {
	nodes, err := c.ListDir(ctx, cid)
	if err != nil {
		return nil, err
	}
	for i := range nodes {
		if nodes[i].Name == name {
			return &nodes[i], nil
		}
	}
	return nil, nil
}

// FindBySize 按大小找文件。115 偶尔会给转存下来的文件改名（去掉非法字符、
// 截断过长标题），名字对不上时用它兜底。
func (c *Client) FindBySize(ctx context.Context, cid string, size int64) (*Node, error) {
	if size <= 0 {
		return nil, nil
	}
	nodes, err := c.ListDir(ctx, cid)
	if err != nil {
		return nil, err
	}
	for i := range nodes {
		if !nodes[i].IsDir && nodes[i].Size == size {
			return &nodes[i], nil
		}
	}
	return nil, nil
}

// WaitFile 轮询等待目录里出现指定文件（离线任务刚完成时 115 有延迟）。
func (c *Client) WaitFile(ctx context.Context, cid, name string, timeout time.Duration) (*Node, error) {
	deadline := time.Now().Add(timeout)
	for {
		n, err := c.FindByName(ctx, cid, name)
		if err == nil && n != nil {
			if !n.IsDir && n.Pickcode == "" {
				if d, err2 := c.Details(ctx, n.ID); err2 == nil {
					n.Pickcode = d.Pickcode
				}
			}
			return n, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("等待文件「%s」超时（115 可能还没转存完成）", name)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
}

// RemoteRelPath 拼出「目录名/文件名」，仅用于展示。
func RemoteRelPath(dirPath, name string) string {
	return path.Join(dirPath, name)
}
