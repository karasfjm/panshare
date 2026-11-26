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

// ShareLink 是解析出来的分享码与提取码。
type ShareLink struct {
	ShareCode   string
	ReceiveCode string
}

// shareCodeRe 匹配 /s/<分享码>：只认 s/ 后面那一段，避免把 115cdn.com
// 这种域名当成分享码（115cdn 正好是 6 位字母数字）。
var shareCodeRe = regexp.MustCompile(`(?i)/s/([a-z0-9]{6,})`)

// bareCodeRe 匹配裸分享码（用户直接粘 swXXXX 这种）。
var bareCodeRe = regexp.MustCompile(`(?i)^([a-z0-9]{6,})$`)

// ParseShare 解析用户粘贴的分享链接。
//
// 支持：
//
//	https://115.com/s/swXXXX?password=abcd
//	https://115cdn.com/s/swXXXX#abcd
//	https://115.com/s/swXXXX-abcd
//	swXXXX-abcd（裸分享码-提取码）
func ParseShare(text string) (ShareLink, error) {
	raw := strings.TrimSpace(text)
	if raw == "" {
		return ShareLink{}, fmt.Errorf("请先粘贴 115 分享链接")
	}
	// 去掉常见前缀与追踪参数
	raw = strings.TrimSpace(strings.Split(raw, "\n")[0])

	var link ShareLink
	if idx := strings.Index(raw, "#"); idx >= 0 {
		link.ReceiveCode = strings.TrimSpace(raw[idx+1:])
		raw = raw[:idx]
	}
	if idx := strings.Index(raw, "?"); idx >= 0 {
		q, err := url.ParseQuery(raw[idx+1:])
		if err == nil {
			if v := q.Get("password"); v != "" {
				link.ReceiveCode = v
			} else if v := q.Get("pwd"); v != "" {
				link.ReceiveCode = v
			}
		}
		raw = raw[:idx]
	}
	raw = strings.TrimRight(raw, "/ ")
	if link.ReceiveCode == "" {
		// 形如 swXXXX-abcd
		if idx := strings.LastIndex(raw, "-"); idx > 0 && !strings.Contains(raw[idx:], "/") {
			link.ReceiveCode = raw[idx+1:]
			raw = raw[:idx]
		}
	}
	m := shareCodeRe.FindStringSubmatch(raw)
	if len(m) < 2 {
		// 没有 /s/ 段：裸分享码，或只粘了路径末尾一段
		tail := raw
		if i := strings.Index(tail, "://"); i >= 0 {
			rest := tail[i+3:]
			if j := strings.IndexAny(rest, "/?#"); j >= 0 {
				tail = rest[j:]
			} else {
				tail = ""
			}
		}
		tail = strings.TrimRight(tail, "/ ")
		if k := strings.LastIndex(tail, "/"); k >= 0 {
			tail = tail[k+1:]
		}
		m = bareCodeRe.FindStringSubmatch(tail)
	}
	if len(m) < 2 {
		return ShareLink{}, fmt.Errorf("没能从链接里认出分享码：%s", text)
	}
	link.ShareCode = m[1]
	return link, nil
}

// ShareItem 是分享里的一个条目。
type ShareItem struct {
	ID       string `json:"id"`
	ParentID string `json:"parent_id"`
	Name     string `json:"name"`
	RelPath  string `json:"rel_path"`
	IsDir    bool   `json:"is_dir"`
	Size     int64  `json:"size"`
	Sha1     string `json:"sha1"`
	Pickcode string `json:"pickcode"`
}

// ShareTree 是一次分享快照遍历的结果。
type ShareTree struct {
	Title     string      `json:"title"`
	Items     []ShareItem `json:"items"`
	TopIDs    []string    `json:"top_ids"`
	FileIDs   []string    `json:"file_ids"`
	FileNum   int         `json:"file_num"`
	DirNum    int         `json:"dir_num"`
	Truncated bool        `json:"truncated"`
}

// ShareSnap 读一页分享目录。
func (c *Client) ShareSnap(ctx context.Context, shareCode, receiveCode, cid string, offset int) (map[string]any, []map[string]any, error) {
	if cid == "" {
		cid = "0"
	}
	q := url.Values{}
	q.Set("share_code", shareCode)
	q.Set("receive_code", receiveCode)
	q.Set("cid", cid)
	q.Set("limit", "100")
	q.Set("offset", fmt.Sprint(offset))
	if receiveCode != "" {
		q.Set("password", receiveCode)
	}
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		m, err := c.getJSON(ctx, WebAPI+pathShareSnap+"?"+q.Encode(), nil)
		if err != nil {
			lastErr = err
			if IsWAF(err) {
				time.Sleep(time.Duration(attempt+1) * 3 * time.Second)
				continue
			}
			return nil, nil, err
		}
		if err := check("读取分享", m); err != nil {
			return nil, nil, err
		}
		d := subMap(m, "data")
		items := listOf(d, "list")
		info := subMap(d, "shareinfo")
		return info, items, nil
	}
	return nil, nil, lastErr
}

// ListShareTree 递归列出分享内容（文件与目录都带上相对路径）。
func (c *Client) ListShareTree(ctx context.Context, shareCode, receiveCode string) (*ShareTree, error) {
	tree := &ShareTree{}
	var walk func(cid, prefix string, depth int) error
	files := 0
	walk = func(cid, prefix string, depth int) error {
		if depth > 32 || files > 200000 {
			tree.Truncated = true
			return nil
		}
		offset := 0
		for {
			info, items, err := c.ShareSnap(ctx, shareCode, receiveCode, cid, offset)
			if err != nil {
				return err
			}
			if t := str(info, "share_title", "title"); t != "" && tree.Title == "" {
				tree.Title = t
			}
			if len(items) == 0 {
				return nil
			}
			for _, it := range items {
				// 分享接口里：目录只有 cid，文件才有 fid / sha
				fid := str(it, "fid", "file_id")
				id := fid
				isDir := fid == "" && str(it, "sha") == ""
				if isDir {
					id = str(it, "cid", "category_id")
				}
				if id == "" {
					continue
				}
				name := str(it, "n", "name", "file_name")
				rel := path.Join(prefix, name)
				item := ShareItem{
					ID: id, ParentID: cid, Name: name, RelPath: rel, IsDir: isDir,
					Size: num(it, "s", "size"), Sha1: strings.ToUpper(str(it, "sha", "sha1")),
					Pickcode: str(it, "pc", "pickcode"),
				}
				if depth == 0 && prefix == "" {
					tree.TopIDs = append(tree.TopIDs, id)
				}
				tree.Items = append(tree.Items, item)
				if isDir {
					tree.DirNum++
					if err := walk(id, rel, depth+1); err != nil {
						return err
					}
				} else {
					files++
					tree.FileNum++
					tree.FileIDs = append(tree.FileIDs, id)
				}
			}
			if len(items) < 100 {
				return nil
			}
			offset += len(items)
			time.Sleep(250 * time.Millisecond)
		}
	}
	if err := walk("0", "", 0); err != nil {
		return tree, err
	}
	return tree, nil
}

// ShareReceive 转存分享里的顶层条目到 cid。
func (c *Client) ShareReceive(ctx context.Context, shareCode, receiveCode string, fileIDs []string, cid string) error {
	if len(fileIDs) == 0 {
		return fmt.Errorf("这个分享里没有可转存的内容")
	}
	form := url.Values{}
	form.Set("share_code", shareCode)
	form.Set("receive_code", receiveCode)
	form.Set("file_id", strings.Join(fileIDs, ","))
	form.Set("cid", cid)
	m, err := c.postForm(ctx, WebAPI+pathShareReceive, form)
	if err != nil {
		return err
	}
	if err := check("转存分享", m); err != nil {
		if e, ok := err.(*Error); ok && (strings.Contains(e.Msg, "已接收") ||
			strings.Contains(e.Msg, "重复接收") || strings.Contains(e.Msg, "已经接收")) {
			return nil // 幂等：之前转存过不算失败
		}
		return err
	}
	return nil
}

// ShareSend 把文件创建成自己的分享，返回分享码与提取码。
func (c *Client) ShareSend(ctx context.Context, fileIDs []string) (string, string, error) {
	if len(fileIDs) == 0 {
		return "", "", fmt.Errorf("没有可分享的文件")
	}
	form := url.Values{}
	form.Set("file_ids", strings.Join(fileIDs, ","))
	form.Set("ignore_warn", "1")
	m, err := c.postForm(ctx, WebAPI+pathShareSend, form)
	if err != nil {
		return "", "", err
	}
	if err := check("创建分享", m); err != nil {
		return "", "", err
	}
	d := subMap(m, "data")
	sc := str(d, "share_code", "share_key")
	rc := str(d, "receive_code")
	if sc == "" {
		return "", "", &Error{Op: "创建分享", Msg: "响应里没有分享码"}
	}
	return sc, rc, nil
}

// ShareUpdate 更新分享设置。duration=-1 表示长期分享。
func (c *Client) ShareUpdate(ctx context.Context, shareCode, receiveCode string, duration int) error {
	form := url.Values{}
	form.Set("share_code", shareCode)
	if receiveCode != "" {
		form.Set("receive_code", receiveCode)
	}
	if duration != 0 {
		form.Set("share_duration", fmt.Sprint(duration))
	}
	m, err := c.postForm(ctx, WebAPI+pathShareUpdate, form)
	if err != nil {
		return err
	}
	return check("更新分享", m)
}

// ShareDelete 取消自己的分享。
func (c *Client) ShareDelete(ctx context.Context, shareCode string) error {
	form := url.Values{}
	form.Set("share_code", shareCode)
	form.Set("action", "delete")
	m, err := c.postForm(ctx, WebAPI+pathShareUpdate, form)
	if err != nil {
		return err
	}
	return check("取消分享", m)
}

// UA115Browser 是 115 客户端的 UA。app 通道（分享直链）只认这一类，
// 拿浏览器 UA 去请求会被判成「版本过低」，回一句没头没尾的「服务器开小差了」。
const UA115Browser = "Mozilla/5.0 115Browser/27.0.5.7"

// ShareDownloadURL 从「自己的分享」里解析直链。
// 这是「strm 指向自己的分享，播放时从分享通道解析」的落地点。
//
// 走 proapi /app/share/downurl：POST ?t=<秒级时间戳>，表单里只有一个 data，
// 内容是 m115 编码的 JSON，返回值同样是 m115 编码的 JSON（{url:{url:...}}）。
func (c *Client) ShareDownloadURL(ctx context.Context, shareCode, receiveCode, fileID string) (string, error) {
	key := m115.GenerateKey()
	payload, err := json.Marshal(map[string]string{
		"share_code":   shareCode,
		"receive_code": receiveCode,
		"file_id":      fileID,
	})
	if err != nil {
		return "", err
	}
	form := url.Values{}
	form.Set("data", m115.Encode(payload, key))
	rawURL := ProAPI + pathAppShareDownurl + "?t=" + strconv.FormatInt(time.Now().Unix(), 10)
	data, _, err := c.do(ctx, rawURL, requestOptions{
		method:    http.MethodPost,
		form:      form,
		ua:        UA115Browser,
		noReferer: true,
		extra:     map[string]string{"Accept": "application/json;charset=UTF-8"},
	})
	if err != nil {
		return "", err
	}
	m, err := parseJSON(data)
	if err != nil {
		return "", err
	}
	if err := check("解析分享直链", m); err != nil {
		return "", err
	}
	// 正常响应：data 是 m115 密文，解出来才是 {fid,fn,sha1,url:{url}}
	if enc := str(m, "data"); enc != "" {
		if raw, err := m115.Decode(enc, key); err == nil {
			var out struct {
				URL struct {
					URL string `json:"url"`
				} `json:"url"`
			}
			if json.Unmarshal(raw, &out) == nil && out.URL.URL != "" {
				return out.URL.URL, nil
			}
			var m2 map[string]any
			if json.Unmarshal(raw, &m2) == nil {
				if u, _ := findURL(m2, 0); u != "" {
					return u, nil
				}
			}
		}
	}
	if u, _ := findURL(m, 0); u != "" {
		return u, nil
	}
	return "", &Error{Op: "解析分享直链", Msg: "响应里没有下载地址"}
}

// ShareStr 给出可展示的分享链接。
func ShareStr(code string) string { return "https://115.com/s/" + code }
