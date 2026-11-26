package p115

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// 这个文件是「接口形状探针」：115 经常整套换掉离线下载 / 分享直链的路径与参数，
// 出错时只看一句「Not Found」「服务器开小差了」根本不知道改哪儿。
// 用 cmd/apiprobe 跑一遍，能直接看到每个变体的状态码与响应体。

type probeResult struct {
	name   string
	url    string
	status int
	body   string
	err    error
}

func (p probeResult) String() string {
	body := strings.TrimSpace(p.body)
	if len(body) > 500 {
		body = body[:500] + "…"
	}
	body = strings.ReplaceAll(body, "\n", " ")
	return fmt.Sprintf("### %s\n%s\nstatus=%d\nerr=%v\nbody=%s\n", p.name, p.url, p.status, p.err, body)
}

// DebugOfflineAdd 试探「提交 ed2k 离线任务」的几种写法。
func (c *Client) DebugOfflineAdd(ctx context.Context, ed2kURL, cid string) string {
	if cid == "" {
		cid = "0"
	}
	var out []probeResult

	get := func(name, rawURL string) {
		data, status, err := c.do(ctx, rawURL, requestOptions{extra: map[string]string{"User-Agent": CloudDownUA}})
		out = append(out, probeResult{name, rawURL, status, string(data), err})
	}

	// 1) 提交：app 通道（clouddownload.115.com/lixianssp），body 是 m115 编码的 data
	m, err := c.cloudDownCall(ctx, URLCloudDownAdd, "add_task_url", map[string]string{
		"url":        ed2kURL,
		"wp_path_id": cid,
	})
	raw, _ := json.Marshal(m)
	out = append(out, probeResult{"clouddownload add_task_url", URLCloudDownAdd, http.StatusOK, string(raw), err})

	// 2) 任务列表 + 单查：确认轮询能拿到状态
	listQ := url.Values{}
	listQ.Set("ac", "task_lists")
	listQ.Set("page", "1")
	listQ.Set("page_size", "10")
	listQ.Set("stat", "0")
	get("clouddownload task_lists", URLCloudDownWeb+"?"+listQ.Encode())

	if _, _, hash, e := ParseEd2k(ed2kURL); e == nil && hash != "" {
		q := url.Values{}
		q.Set("ac", "get_user_task")
		q.Set("info_hash", hash)
		get("clouddownload get_user_task", URLCloudDownWeb+"?"+q.Encode())
	}

	// 3) 老网关留一份对照，方便下次再变时一眼看出是新旧哪边挂了
	oldForm := url.Values{}
	oldForm.Set("urls", ed2kURL)
	oldForm.Set("wp_path_id", cid)
	if data, status, e := c.do(ctx, "https://115.com/web/lixian/?ct=lixian&ac=add_task_urls",
		requestOptions{method: http.MethodPost, form: oldForm}); e == nil {
		out = append(out, probeResult{"115.com/web/lixian add_task_urls（旧）", "https://115.com/web/lixian/", status, string(data), nil})
	}

	return joinProbes(out)
}

// DebugShareDownload 试探「从自己的分享解析直链」的几种写法。
func (c *Client) DebugShareDownload(ctx context.Context, shareCode, receiveCode, fileID string) string {
	var out []probeResult

	getRef := func(name, rawURL, referer string) {
		data, status, err := c.do(ctx, rawURL, requestOptions{referer: referer})
		out = append(out, probeResult{name, rawURL, status, string(data), err})
	}
	get := func(name, rawURL string) { getRef(name, rawURL, "") }
	post := func(name, rawURL string, f url.Values) {
		data, status, err := c.do(ctx, rawURL, requestOptions{method: http.MethodPost, form: f})
		out = append(out, probeResult{name, rawURL, status, string(data), err})
	}
	postJSONBody := func(name, rawURL, body string) {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, rawURL, strings.NewReader(body))
		if err != nil {
			out = append(out, probeResult{name, rawURL, 0, "", err})
			return
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("User-Agent", c.UA)
		req.Header.Set("Cookie", c.Cookies)
		req.Header.Set("Referer", "https://115.com/")
		resp, err := c.http.Do(req)
		if err != nil {
			out = append(out, probeResult{name, rawURL, 0, "", err})
			return
		}
		defer resp.Body.Close()
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		out = append(out, probeResult{name, rawURL, resp.StatusCode, string(data), nil})
	}

	// 先把分享根目录列出来：分享里每条有自己的 fid/cid，和账号里的文件 id 不是一回事
	sq := url.Values{}
	sq.Set("share_code", shareCode)
	sq.Set("receive_code", receiveCode)
	sq.Set("limit", "10")
	sq.Set("offset", "0")
	if snap, status, err := c.do(ctx, WebAPI+pathShareSnap+"?"+sq.Encode(), requestOptions{}); err == nil {
		out = append(out, probeResult{"分享根目录快照", WebAPI + pathShareSnap + "?" + sq.Encode(), status, string(snap), nil})
	} else {
		out = append(out, probeResult{"分享根目录快照", WebAPI + pathShareSnap + "?" + sq.Encode(), status, string(snap), err})
	}

	q := url.Values{}
	q.Set("share_code", shareCode)
	q.Set("receive_code", receiveCode)
	q.Set("file_id", fileID)
	get("webapi /share/download", WebAPI+"/share/download?"+q.Encode())
	getRef("webapi /share/download +分享页 referer", WebAPI+"/share/download?"+q.Encode(),
		"https://115.com/s/"+shareCode)

	q2 := url.Values{}
	q2.Set("share_code", shareCode)
	q2.Set("file_id", fileID)
	get("webapi /share/download 无 receive_code", WebAPI+"/share/download?"+q2.Encode())

	q3 := url.Values{}
	q3.Set("share_code", shareCode)
	q3.Set("receive_code", receiveCode)
	q3.Set("file_id", fileID)
	q3.Set("n", "1")
	get("webapi /share/download +n", WebAPI+"/share/download?"+q3.Encode())

	post("webapi /share/download (POST form)", WebAPI+"/share/download", q)

	// app 通道：JSON body，参数同名
	body := fmt.Sprintf(`{"share_code":%s,"receive_code":%s,"file_id":%s}`,
		mustJSON(shareCode), mustJSON(receiveCode), mustJSON(fileID))
	postJSONBody("proapi /app/2.0/share/downurl (json)", ProAPI+"/app/2.0/share/downurl", body)

	post("proapi /app/2.0/share/downurl (form)", ProAPI+"/app/2.0/share/downurl", q)

	return joinProbes(out)
}

func joinProbes(list []probeResult) string {
	var b strings.Builder
	for _, p := range list {
		b.WriteString(p.String())
		b.WriteString("\n")
	}
	return b.String()
}

func mustJSON(v string) string {
	data, _ := json.Marshal(v)
	return string(data)
}

// DumpShareSnap 原样打印分享根目录（含每条 item 的 fid / cid / 名字 / pickcode），
// 用来确认「分享里的 file_id」到底长什么样。
func (c *Client) DumpShareSnap(ctx context.Context, shareCode, receiveCode string) string {
	q := url.Values{}
	q.Set("share_code", shareCode)
	q.Set("receive_code", receiveCode)
	q.Set("limit", "20")
	q.Set("offset", "0")
	data, status, err := c.do(ctx, WebAPI+pathShareSnap+"?"+q.Encode(), requestOptions{})
	if err != nil {
		return fmt.Sprintf("status=%d err=%v\n%s", status, err, string(data))
	}
	var m map[string]any
	pretty := string(data)
	if json.Unmarshal(data, &m) == nil {
		if b, err := json.MarshalIndent(m, "", "  "); err == nil {
			pretty = string(b)
		}
	}
	return fmt.Sprintf("status=%d\n%s", status, pretty)
}

// DebugUploadInit 一次性试几组参数组合，直接看哪组能让 115 认秒传。
// 每组只用同一份源文件 sha1/size，除了被试探的字段以外完全一样。
func (c *Client) DebugUploadInit(ctx context.Context, sha1sum, name string, size int64, cid string) string {
	var b strings.Builder
	key, err := c.UploadKey(ctx)
	if err != nil {
		return "UploadKey: " + err.Error()
	}
	uid := c.UID(ctx)
	fileid := strings.ToUpper(sha1sum)
	target := "U_1_" + cid
	fmt.Fprintf(&b, "uid=%s userkey=%s fileid=%s size=%d target=%s\n\n", uid, key, fileid, size, target)

	type variant struct {
		label   string
		appver  string
		appid   string // 空表示不带
		ms      bool   // t 用毫秒
		keepSig bool   // 带上空的 sign_key/sign_val
	}
	variants := []variant{
		{"① p115client 原样（秒 / 无 appid / 36.2.28）", "36.2.28", "", false, false},
		{"② 加 appid=0", "36.2.28", "0", false, false},
		{"③ 带空 sign_key/sign_val", "36.2.28", "", false, true},
		{"④ 毫秒 t + 27.0.5.7", "27.0.5.7", "", true, false},
	}

	for _, v := range variants {
		form, _ := uploadForm(uid, key, fileid, name, size, target, v.appver)
		if v.appid != "" {
			form["appid"] = v.appid
		}
		if v.keepSig {
			form["sign_key"] = ""
			form["sign_val"] = ""
		}
		ts := time.Now().Unix()
		if v.ms {
			ts = time.Now().UnixMilli()
		}
		stampToken(form, uid, fileid, v.appver, strconv.FormatInt(ts, 10))

		enc, e := newUploadCipher()
		if e != nil {
			fmt.Fprintf(&b, "%s\n  生成密钥失败: %v\n", v.label, e)
			continue
		}
		body, e := encodeUploadPayload(enc, form)
		if e != nil {
			fmt.Fprintf(&b, "%s\n  编码失败: %v\n", v.label, e)
			continue
		}
		u := UploadAPI + pathUploadInit + "?k_ec=" + url.QueryEscape(enc.encodeToken(ts))
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
		req.Header.Set("User-Agent", uploadUA)
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("Cookie", c.Cookies)
		resp, e := c.http.Do(req)
		if e != nil {
			fmt.Fprintf(&b, "%s\n  请求失败: %v\n", v.label, e)
			continue
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()

		fmt.Fprintf(&b, "%s HTTP %d len=%d\n", v.label, resp.StatusCode, len(raw))
		if plain, de := enc.decrypt(raw); de != nil {
			fmt.Fprintf(&b, "  解密失败: %v\n  原文: %s\n", de,
				truncate(strings.ToValidUTF8(string(raw), "?"), 300))
		} else {
			fmt.Fprintf(&b, "  明文: %s\n", truncate(strings.ToValidUTF8(string(plain), "?"), 400))
		}
		b.WriteString("\n")
	}
	return b.String()
}

// DebugUploadCreds 对比几处能取 userkey 的接口，并打印 uid 的几种取法。
// 秒传签名里的 userkey/userid 只要有一个来源不对，115 就只回一句 sig invalid。
func (c *Client) DebugUploadCreds(ctx context.Context) string {
	var b strings.Builder
	fmt.Fprintf(&b, "c.UID=%q cookieUID=%q\n", c.UID(ctx), cookieUID(c.Cookies))
	fmt.Fprintf(&b, "cookie 名字：%s\n", cookieNames(c.Cookies))

	do := func(label, method, u string, body []byte, ctype string) {
		var rd io.Reader
		if body != nil {
			rd = bytes.NewReader(body)
		}
		req, err := http.NewRequestWithContext(ctx, method, u, rd)
		if err != nil {
			fmt.Fprintf(&b, "[%s] 构造失败：%v\n", label, err)
			return
		}
		req.Header.Set("User-Agent", uploadUA)
		if ctype != "" {
			req.Header.Set("Content-Type", ctype)
		}
		if c.Cookies != "" {
			req.Header.Set("Cookie", c.Cookies)
		}
		resp, err := c.http.Do(req)
		if err != nil {
			fmt.Fprintf(&b, "[%s] err=%v\n", label, err)
			return
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		resp.Body.Close()
		fmt.Fprintf(&b, "[%s] HTTP %d\n  %s\n", label, resp.StatusCode,
			truncate(strings.ToValidUTF8(string(raw), "?"), 600))
	}

	nowSec := strconv.FormatInt(time.Now().Unix(), 10)
	nowMs := strconv.FormatInt(time.Now().UnixMilli(), 10)

	do("A webapi GET /files/uploadinfo", "GET", WebAPI+pathUploadUserkey, nil, "")
	do("B proapi POST /app/uploadinfo", "POST", ProAPI+"/app/uploadinfo?t="+nowMs, nil,
		"application/x-www-form-urlencoded")
	do("C proapi GET /app/uploadinfo", "GET", ProAPI+"/app/uploadinfo?t="+nowMs, nil, "")
	do("D proapi GET /app/uploadinfo?ac=upload", "GET",
		ProAPI+"/app/uploadinfo?ac=upload&t="+nowSec, nil, "")
	do("E uplb POST /3.0/getuploadinfo.php", "POST", UploadAPI+pathUploadInfo+"?t="+nowMs,
		nil, "application/x-www-form-urlencoded")
	return b.String()
}

// cookieNames 只列出 cookie 的键名，值不落日志。
func cookieNames(cookies string) string {
	var names []string
	for _, part := range strings.Split(cookies, ";") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 && kv[0] != "" {
			names = append(names, kv[0])
		}
	}
	return strings.Join(names, ",")
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
