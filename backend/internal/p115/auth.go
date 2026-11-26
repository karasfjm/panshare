package p115

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// QR 是一次扫码登录会话。
type QR struct {
	UID     string
	Sign    string
	Time    string
	Content string // 二维码里要编的字符串，前端用它画图
}

// anonGetJSON 请求不需要登录态的接口（扫码用的 qrcodeapi）。
func (c *Client) anonGetJSON(ctx context.Context, rawURL string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", c.UA)
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Referer", "https://115.com/")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	return parseJSON(data)
}

// QRToken 取二维码：第一步。
func (c *Client) QRToken(ctx context.Context) (*QR, error) {
	m, err := c.anonGetJSON(ctx, QRCodeAPI+pathQRToken)
	if err != nil {
		return nil, err
	}
	if err := check("获取二维码", m); err != nil {
		return nil, err
	}
	d := subMap(m, "data")
	qr := &QR{
		UID:     str(d, "uid"),
		Sign:    str(d, "sign"),
		Time:    str(d, "time"),
		Content: str(d, "qrcode"),
	}
	if qr.UID == "" {
		return nil, &Error{Op: "获取二维码", Msg: "响应里没有 uid"}
	}
	if qr.Content == "" {
		qr.Content = "https://115.com/scan/dg/" + qr.UID
	}
	return qr, nil
}

// QRStatus 轮询扫码结果。
// 返回 state：waiting / scanned / confirmed；confirmed 时 cookies 有值。
func (c *Client) QRStatus(ctx context.Context, qr *QR) (string, string, error) {
	q := url.Values{}
	q.Set("uid", qr.UID)
	q.Set("time", qr.Time)
	q.Set("sign", qr.Sign)
	m, err := c.anonGetJSON(ctx, QRCodeAPI+pathQRStatus+"?"+q.Encode())
	if err != nil {
		return "", "", err
	}
	if err := check("扫码状态", m); err != nil {
		return "", "", err
	}
	d := subMap(m, "data")
	switch int(num(d, "status")) {
	case 0:
		return "waiting", "", nil
	case 1:
		return "scanned", "", nil
	case 2:
		// 手机确认后，115 会在这条响应里直接给 UID/CID/SEID/KID
		uid := str(d, "uid")
		cid := str(d, "cid")
		seid := str(d, "seid")
		kid := str(d, "kid")
		if uid == "" || cid == "" || seid == "" {
			return "", "", &Error{Op: "扫码登录", Msg: "已确认但没拿到登录凭据，请改用 Cookie 登录"}
		}
		parts := []string{"UID=" + uid, "CID=" + cid, "SEID=" + seid}
		if kid != "" {
			parts = append(parts, "KID="+kid)
		}
		return "confirmed", strings.Join(parts, "; "), nil
	default:
		return "waiting", "", nil
	}
}

// UserInfo 读取当前账号的 uid 与昵称。
func (c *Client) UserInfo(ctx context.Context) (string, string, error) {
	m, err := c.getJSON(ctx, WebAPI+pathUserInfo, nil)
	if err == nil {
		if err2 := check("读取账号信息", m); err2 == nil {
			d := subMap(m, "data")
			if len(d) == 0 {
				d = m
			}
			uid := str(d, "user_id", "uid", "id")
			name := str(d, "user_name", "uname", "nickname", "name")
			if uid != "" {
				if clean := CleanUID(uid); clean != "" {
					return clean, name, nil
				}
				return uid, name, nil
			}
		}
	}
	// /user/info 不可用时，用一次最小目录请求验证登录态，uid 从 cookies 里取
	if err2 := c.Ping(ctx); err2 != nil {
		if err != nil {
			return "", "", err
		}
		return "", "", err2
	}
	return cookieUID(c.Cookies), "", nil
}

// Ping 用最小的目录请求验证 cookies 是否有效。
func (c *Client) Ping(ctx context.Context) error {
	q := url.Values{}
	q.Set("aid", "1")
	q.Set("cid", "0")
	q.Set("limit", "1")
	q.Set("show_dir", "1")
	q.Set("offset", "0")
	m, err := c.getJSON(ctx, WebAPI+pathFiles+"?"+q.Encode(), nil)
	if err != nil {
		return err
	}
	return check("校验登录", m)
}

// UID 返回 115 用户 ID：写进 strm 末尾、按 uid 找账号播放都靠它。
func (c *Client) UID(ctx context.Context) string {
	c.uidMu.Lock()
	cached := c.uid
	c.uidMu.Unlock()
	if cached != "" {
		return cached
	}
	uid, _, err := c.UserInfo(ctx)
	if err != nil || uid == "" {
		uid = cookieUID(c.Cookies)
	}
	uid = CleanUID(uid)
	if uid != "" {
		c.uidMu.Lock()
		c.uid = uid
		c.uidMu.Unlock()
	}
	return uid
}

func cookieUID(cookies string) string {
	for _, part := range strings.Split(cookies, ";") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 && strings.EqualFold(kv[0], "UID") {
			return CleanUID(kv[1])
		}
	}
	return ""
}

// CleanUID 只保留 115 用户 ID 前面的数字。
//
// 115 的 UID cookie 是一个复合串，常见形态（同一个号）：
//
//	1794486118
//	2806858_R1_1794486118
//	A2806858_R1_1794486118
//
// 真正能用来认账号、写进 strm 的是最前面那段数字，后面紧跟的是
// 客户端标识与登录时间戳。不剪掉会让 strm 的地址又长又乱，
// 而且换个客户端登录后同一账号的 UID 就变了，旧 strm 立刻失配。
func CleanUID(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" {
		return ""
	}
	start := -1
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= '0' && c <= '9' {
			if start < 0 {
				start = i
			}
			continue
		}
		if start >= 0 {
			return s[start:i]
		}
		// 数字段之前只允许是空白或单个字母前缀（如 A2806858_...）
		if !(c == ' ' || c == '\t' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) {
			return ""
		}
	}
	if start < 0 {
		return ""
	}
	return s[start:]
}

// NormalizeCookies 把用户粘贴的内容（整条 Cookie: 头、网页版一键复制的串）规整成
// 115 需要的 cookie 串。
func NormalizeCookies(raw string) (string, error) {
	text := strings.TrimSpace(raw)
	text = strings.TrimPrefix(text, "Cookie:")
	text = strings.TrimPrefix(text, "cookie:")
	text = strings.TrimSpace(text)
	text = strings.ReplaceAll(text, "\n", " ")
	text = strings.ReplaceAll(text, "\r", "")
	text = strings.Join(strings.Fields(text), " ")
	if text == "" {
		return "", fmt.Errorf("cookie 不能为空")
	}
	hasUID := false
	for _, part := range strings.Split(text, ";") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 && strings.EqualFold(kv[0], "UID") {
			hasUID = true
		}
	}
	if !hasUID {
		return "", fmt.Errorf("这份 cookie 里没有 UID：请从浏览器登录 115 后复制整条 Cookie")
	}
	return text, nil
}
