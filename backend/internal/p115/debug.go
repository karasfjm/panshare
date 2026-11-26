package p115

// 直链接口的排查辅助：把每个请求的原始响应、解密结果都摊开。
// 只在 cmd/p115probe 里用，正常业务不会走这里。

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DebugDownload 逐步跑一遍取直链的流程并返回文字痕迹。
func (c *Client) DebugDownload(ctx context.Context, pickcode string) string {
	var b strings.Builder
	ua := c.UA
	_ = ua

	step := func(name, rawURL, payload string) {
		form := url.Values{}
		form.Set("data", encryptPayload([]byte(payload)))
		body, hdr, err := c.postFormRaw(ctx, rawURL, form, nil)
		fmt.Fprintf(&b, "\n### %s\n", name)
		fmt.Fprintf(&b, "payload=%s\n", payload)
		fmt.Fprintf(&b, "err=%v\n", err)
		if hdr != nil {
			if sc := hdr.Values("Set-Cookie"); len(sc) > 0 {
				fmt.Fprintf(&b, "Set-Cookie=%v\n", sc)
			}
		}
		fmt.Fprintf(&b, "body=%s\n", truncate(strings.TrimSpace(string(body)), 500))
		m, perr := parseJSON(body)
		if perr != nil {
			fmt.Fprintf(&b, "parseErr=%v\n", perr)
			return
		}
		if d, ok := m["data"].(string); ok && d != "" {
			if dir := os.Getenv("P115_DEBUG_DIR"); dir != "" {
				name := strings.NewReplacer("/", "_", " ", "_").Replace(name)
				_ = os.WriteFile(filepath.Join(dir, "resp_"+name+".txt"), []byte(d), 0o644)
			}
			plain, derr := decryptPayload(d)
			fmt.Fprintf(&b, "decryptErr=%v\nplain=%s\n", derr, truncate(string(plain), 500))
			if derr == nil {
				if dm, e := decryptJSON(d); e == nil {
					u, _ := findURL(dm, 0)
					fmt.Fprintf(&b, "foundURL=%q\n", truncate(u, 200))
				}
			}
		}
	}

	step("web /files/download (加密)", WebAPI+pathFilesDown,
		fmt.Sprintf(`{"pickcode":%q}`, pickcode))
	step("proapi /app/chrome/downurl (加密)", ProAPI+pathChromeDown,
		fmt.Sprintf(`{"pickcode":%q,"user_id":%q}`, pickcode, c.UID(ctx)))
	return b.String()
}

// DebugLife 逐个试探生活事件接口的真实路径，返回文字痕迹。
// 115 这套接口换过好几版域名/前缀，出问题时先跑这个确认走哪条。
func (c *Client) DebugLife(ctx context.Context, limit int) string {
	if limit <= 0 {
		limit = 5
	}
	today := time.Now().Format("2006-01-02")
	var b strings.Builder
	try := func(name, rawURL string) {
		data, status, err := c.do(ctx, rawURL, requestOptions{})
		fmt.Fprintf(&b, "\n### %s\n%s\nstatus=%d err=%v\nbody=%s\n", name, rawURL, status, err, truncate(strings.TrimSpace(string(data)), 400))
	}
	try("proapi app/2.0", ProAPI+"/app/2.0/behavior/detail?limit="+itoa(limit)+"&offset=0")
	try("proapi app/2.0 +date", ProAPI+"/app/2.0/behavior/detail?limit="+itoa(limit)+"&offset=0&date="+today)
	try("proapi app/3.0", ProAPI+"/app/3.0/behavior/detail?limit="+itoa(limit)+"&offset=0")
	try("proapi 2.0", ProAPI+"/2.0/behavior/detail?limit="+itoa(limit)+"&offset=0")
	try("webapi behavior/detail", WebAPI+"/behavior/detail?limit="+itoa(limit)+"&offset=0")
	try("webapi behavior/detail +date", WebAPI+"/behavior/detail?limit="+itoa(limit)+"&offset=0&date="+today)
	try("webapi life/detail", WebAPI+"/life/detail?limit="+itoa(limit)+"&offset=0")
	try("webapi life/list", WebAPI+"/life/list?limit="+itoa(limit)+"&offset=0")
	try("webapi files/history", WebAPI+"/files/history?limit="+itoa(limit))
	return b.String()
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [12]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

// DebugPath 试探「目录 cid → 网盘路径」的取法。事件只给 parent_id，
// 拼不出路径就没法判断这个文件在不在监控目录里，所以这条路必须通。
func (c *Client) DebugPath(ctx context.Context, cid string) string {
	var b strings.Builder
	try := func(name, rawURL string) {
		data, status, err := c.do(ctx, rawURL, requestOptions{})
		fmt.Fprintf(&b, "\n### %s\n%s\nstatus=%d err=%v\nbody=%s\n", name, rawURL, status, err, truncate(strings.TrimSpace(string(data)), 300))
	}
	try("files/details file_id", WebAPI+pathFilesDetails+"?file_id="+cid)
	try("files/details cid", WebAPI+pathFilesDetails+"?cid="+cid)
	try("files/getpath file_id", WebAPI+"/files/getpath?file_id="+cid)
	try("files/getpath cid", WebAPI+"/files/getpath?cid="+cid)
	try("files/getpath_collection", WebAPI+"/files/getpath?file_id="+cid+"&_=1")
	try("files list self", WebAPI+"/files?cid="+cid+"&show_dir=1&limit=3")
	return b.String()
}

// DebugLifeRaw 原样返回 behavior/detail 的响应，用来确认事件里到底有哪些字段。
func (c *Client) DebugLifeRaw(ctx context.Context, limit int) string {
	data, status, err := c.do(ctx, WebAPI+pathBehaviorDetail+"?limit="+itoa(limit)+"&offset=0", requestOptions{})
	return fmt.Sprintf("status=%d err=%v\n%s", status, err, string(data))
}

// DebugDelete 试探删除接口的参数形状：115 对 fid 的写法比较挑，
// 传错就回「服务器开小差了」。
func (c *Client) DebugDelete(ctx context.Context, id, pid string) string {
	var b strings.Builder
	try := func(name, rawURL string, form url.Values) {
		m, err := c.postForm(ctx, rawURL, form)
		fmt.Fprintf(&b, "\n### %s\nform=%v\nerr=%v\nresp=%v\n", name, form, err, m)
	}
	f1 := url.Values{}
	f1.Set("pid", pid)
	f1.Set("fid[0]", id)
	try("files/delete fid[0]", WebAPI+pathFilesDelete, f1)

	f2 := url.Values{}
	f2.Set("pid", pid)
	f2.Set("fid", id)
	try("files/delete fid", WebAPI+pathFilesDelete, f2)

	f3 := url.Values{}
	f3.Set("pid", pid)
	f3.Set("fid[0]", id)
	f3.Set("_", "1")
	try("files/delete fid[0]+_", WebAPI+pathFilesDelete, f3)

	f4 := url.Values{}
	f4.Set("fid", id)
	f4.Set("pid", pid)
	f4.Set("aid", "1")
	try("files/delete aid", WebAPI+pathFilesDelete, f4)

	f5 := url.Values{}
	f5.Set("fid[0]", id)
	try("files/delete 只给 fid[0]", WebAPI+pathFilesDelete, f5)

	f6 := url.Values{}
	f6.Set("fid[0]", id)
	f6.Set("pid", "0")
	try("files/delete pid=0", WebAPI+pathFilesDelete, f6)

	f7 := url.Values{}
	f7.Set("fid[]", id)
	f7.Set("pid", pid)
	try("files/delete fid[]", WebAPI+pathFilesDelete, f7)
	return b.String()
}

// DebugDelete2 再试两种删除写法：回收站接口 / JSON body。
func (c *Client) DebugDelete2(ctx context.Context, id, pid string) string {
	var b strings.Builder
	// rb/delete
	f := url.Values{}
	f.Set("fid[0]", id)
	f.Set("pid", pid)
	if m, err := c.postForm(ctx, WebAPI+"/rb/delete", f); err != nil {
		fmt.Fprintf(&b, "rb/delete err=%v\n", err)
	} else {
		fmt.Fprintf(&b, "rb/delete resp=%v\n", m)
	}
	// JSON body
	body := fmt.Sprintf(`{"fid":["%s"],"pid":"%s"}`, id, pid)
	if m, err := c.postJSON(ctx, WebAPI+pathFilesDelete, body); err != nil {
		fmt.Fprintf(&b, "json err=%v\n", err)
	} else {
		fmt.Fprintf(&b, "json resp=%v\n", m)
	}
	return b.String()
}
