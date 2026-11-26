// p115probe 用真实账号跑一遍「取直链」的生产代码路径，排查播放失败用。
//
// 用法（在容器里跑，容器内已有 /config）：
//
//	p115probe -db /config/panshare.db -pickcode xxxxxxxx [-acc 0]
//
// 不走网络以外的任何东西，也不打印 cookie 内容。
package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"panshare/internal/p115"
)

func main() {
	db := flag.String("db", "/config/panshare.db", "sqlite 路径")
	pickcode := flag.String("pickcode", "", "要取直链的 pickcode")
	accIdx := flag.Int("acc", -1, "第几个账号，-1 表示全部试")
	dbg := flag.Bool("debug", false, "打印每个请求的原始响应与解密结果")
	api := flag.String("api", "", "取直链通道：android / chrome / web，留空按 android→chrome→web 依次试")
	var uas multiFlag
	flag.Var(&uas, "ua", "指定 UA，可重复；不给就用内置播放器 UA")
	flag.Parse()
	if *pickcode == "" {
		fmt.Println("必须给 -pickcode")
		os.Exit(2)
	}
	if len(uas) == 0 {
		uas = multiFlag{p115.UserAgent}
	}

	accs, err := loadAccounts(*db)
	if err != nil || len(accs) == 0 {
		fmt.Println("读账号失败:", err)
		os.Exit(1)
	}
	ctx := context.Background()
	for i, a := range accs {
		if *accIdx >= 0 && i != *accIdx {
			continue
		}
		c := p115.New(a.cookies)
		c.SetQPS(1)
		uid := c.UID(ctx)
		fmt.Printf("账号 #%d %s uid=%q\n", i, a.name, uid)

		if *dbg {
			fmt.Println(c.DebugDownload(ctx, *pickcode))
		}

		for _, ua := range uas {
			start := time.Now()
			link, hdr, err := c.DownloadURLVia(ctx, *pickcode, ua, *api, 1*time.Minute)
			took := time.Since(start).Round(time.Millisecond)
			if err != nil {
				fmt.Printf("  api=%q 失败 (%s): %v\n", *api, took, err)
				continue
			}
			fmt.Printf("  api=%q UA=%q\n  f=%q  成功(%s)\n  url=%s\n", *api, ua, fParam(link), took, link)
			if len(hdr) > 0 {
				fmt.Printf("  附加请求头: %v\n", keys(hdr))
			}
			// 模拟播放器：只带 UA 拉前 1KB，看 CDN 是否放行
			checkRange(link, ua, nil, "只带 UA")
			if ck := hdr["Cookie"]; ck != "" {
				checkRange(link, ua, map[string]string{"Cookie": ck}, "UA+Cookie(响应里给的)")
			}
			checkRange(link, ua, map[string]string{"Cookie": a.cookies}, "UA+账号Cookie")
		}
	}
}

func checkRange(link, ua string, hdr map[string]string, label string) {
	req, err := http.NewRequest(http.MethodGet, link, nil)
	if err != nil {
		fmt.Println("  ", label, "构造失败", err)
		return
	}
	req.Header.Set("User-Agent", ua)
	req.Header.Set("Range", "bytes=0-1023")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		fmt.Println("  ", label, "请求失败", err)
		return
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 200))
	fmt.Printf("  拉流[%s] → HTTP %d  len=%d  %s\n", label, resp.StatusCode, len(b), firstLine(b))
}

func firstLine(b []byte) string {
	s := strings.TrimSpace(string(b))
	if i := strings.IndexByte(s, '\n'); i > 0 {
		s = s[:i]
	}
	if len(s) > 80 {
		s = s[:80]
	}
	return s
}

func keys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// multiFlag 让 -ua 可以重复出现。
type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ", ") }
func (m *multiFlag) Set(v string) error {
	*m = append(*m, v)
	return nil
}

func fParam(link string) string {
	u, err := url.Parse(link)
	if err != nil {
		return "?"
	}
	if f := u.Query().Get("f"); f != "" {
		return f
	}
	return "无"
}

type account struct{ name, uid, cookies string }

func loadAccounts(path string) ([]account, error) {
	db, err := sql.Open("sqlite", "file:"+path+"?mode=ro")
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.Query(`SELECT name, uid, cookies FROM accounts
		WHERE cookies <> '' ORDER BY is_default DESC, created_at ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []account
	for rows.Next() {
		var a account
		if err := rows.Scan(&a.name, &a.uid, &a.cookies); err != nil {
			return nil, err
		}
		a.cookies = strings.TrimSpace(a.cookies)
		out = append(out, a)
	}
	return out, rows.Err()
}
