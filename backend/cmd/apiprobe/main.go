// apiprobe 用真实账号试探 115 接口的真实形状（离线任务 / 分享直链）。
//
// 用法（在容器里跑）：
//
//	apiprobe -db /config/panshare.db -mode offline -ed2k "ed2k://|file|..."
//	apiprobe -db /config/panshare.db -mode share -share swxxxx -rc abcd -file 3566...
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
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"panshare/internal/p115"
)

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

func main() {
	db := flag.String("db", "/config/panshare.db", "sqlite 路径")
	accIdx := flag.Int("acc", 0, "第几个账号")
	mode := flag.String("mode", "share", "offline / add / share / snap / impl")
	ed2k := flag.String("ed2k", "", "mode=offline 时要提交的 ed2k 链接")
	cid := flag.String("cid", "0", "mode=offline 时的目标目录 cid")
	share := flag.String("share", "", "mode=share 时的分享码")
	rc := flag.String("rc", "", "mode=share 时的提取码")
	file := flag.String("file", "", "mode=share 时的文件 id")
	pick := flag.String("pick", "", "mode=instant 时源文件的 pickcode")
	to := flag.Int("to", -1, "mode=instant 时目标账号下标")
	flag.Parse()

	accs, err := loadAccounts(*db)
	if err != nil || len(accs) == 0 {
		fmt.Println("读账号失败:", err)
		os.Exit(1)
	}
	if *accIdx < 0 || *accIdx >= len(accs) {
		fmt.Printf("账号下标越界（共 %d 个）\n", len(accs))
		os.Exit(2)
	}
	a := accs[*accIdx]
	c := p115.New(a.cookies)
	c.SetQPS(1)
	ctx := context.Background()
	fmt.Printf("账号 #%d %s uid=%q\n\n", *accIdx, a.name, c.UID(ctx))

	switch *mode {
	case "offline":
		if *ed2k == "" {
			fmt.Println("mode=offline 需要 -ed2k")
			os.Exit(2)
		}
		fmt.Println(c.DebugOfflineAdd(ctx, *ed2k, *cid))
	case "status":
		// 只看状态，不提交任何任务
		if *ed2k == "" {
			fmt.Println("mode=status 需要 -ed2k")
			os.Exit(2)
		}
		_, _, hash, e := p115.ParseEd2k(*ed2k)
		if e != nil {
			fmt.Println("解析链接失败:", e)
			os.Exit(2)
		}
		t, err := c.OfflineTaskInfo(ctx, hash)
		fmt.Printf("单查：%+v err=%v\n", t, err)
		if list, err := c.OfflineList(ctx); err == nil {
			for _, it := range list {
				if it.InfoHash == hash {
					fmt.Printf("列表里：%s %d%% %s\n", it.Status, it.Percent, it.StatusTxt)
				}
			}
		}
	case "add":
		// 跑真实的 OfflineAdd（含 10008 删记录重提），验证业务链路而不是探针
		if *ed2k == "" {
			fmt.Println("mode=add 需要 -ed2k")
			os.Exit(2)
		}
		err := c.OfflineAdd(ctx, *ed2k, *cid)
		fmt.Printf("OfflineAdd err=%v\n", err)
		if _, _, hash, e := p115.ParseEd2k(*ed2k); e == nil {
			if t, e2 := c.OfflineTaskInfo(ctx, hash); e2 == nil && t != nil {
				fmt.Printf("任务状态：%s %d%% %s\n", t.Status, t.Percent, t.StatusTxt)
			} else {
				fmt.Printf("查询任务：%v %v\n", t, e2)
			}
		}
	case "snap":
		if *share == "" {
			fmt.Println("mode=snap 需要 -share")
			os.Exit(2)
		}
		fmt.Println(c.DumpShareSnap(ctx, *share, *rc))
	case "impl":
		if *share == "" || *file == "" {
			fmt.Println("mode=impl 需要 -share 和 -file")
			os.Exit(2)
		}
		link, err := c.ShareDownloadURL(ctx, *share, *rc, *file)
		fmt.Printf("link=%s\nerr=%v\n", link, err)
	case "share":
		if *share == "" || *file == "" {
			fmt.Println("mode=share 需要 -share 和 -file")
			os.Exit(2)
		}
		fmt.Println(c.DebugShareDownload(ctx, *share, *rc, *file))
	case "init":
		fmt.Println(c.DebugUploadInit(ctx, "8CD1E7CA93A184220981203A5A0C47146A15E563",
			"ZERO 一获千金游戏 - S02E04 - 第4集.mp4", 412751677, *cid))
	case "upload":
		fmt.Println(probeUpload(ctx, a.cookies, c.UID(ctx), "8CD1E7CA93A184220981203A5A0C47146A15E563",
			"ZERO 一获千金游戏 - S02E04 - 第4集.mp4", 412751677, *cid))
	case "creds":
		fmt.Println(c.DebugUploadCreds(ctx))
	case "instant":
		if *pick == "" || *to < 0 || *to >= len(accs) {
			fmt.Println("mode=instant 需要 -pick <源 pickcode> -to <目标账号下标> [-cid 目录]")
			os.Exit(2)
		}
		fmt.Println(probeInstant(ctx, *db, accs, c, *pick, *to, *cid))
	default:
		fmt.Println("未知 mode:", *mode)
		os.Exit(2)
	}
}

// probeInstant 跑一次真实的跨账号秒传：源文件直链取校验块 → 目标账号秒传到 cid。
func probeInstant(ctx context.Context, dbPath string, accs []account, source *p115.Client, pickcode string, toIdx int, cid string) string {
	var b strings.Builder
	db, err := sql.Open("sqlite", "file:"+dbPath+"?mode=ro")
	if err != nil {
		return "打开数据库失败: " + err.Error()
	}
	defer db.Close()

	var name, sha1, accountID string
	var size int64
	err = db.QueryRow(`SELECT name, sha1, size, account_id FROM strm_files
		WHERE pickcode = ? AND deleted = 0 LIMIT 1`, pickcode).Scan(&name, &sha1, &size, &accountID)
	if err != nil {
		return "查文件失败: " + err.Error()
	}
	fmt.Fprintf(&b, "源文件：%s\n  sha1=%s size=%d 源账号=%s\n", name, sha1, size, accountID)

	target := p115.New(accs[toIdx].cookies)
	target.SetQPS(1)
	fmt.Fprintf(&b, "目标账号 #%d %s uid=%q\n", toIdx, accs[toIdx].name, target.UID(ctx))

	// 先取上传凭据与初始化，看 115 到底要什么
	info, ierr := target.UploadInfo(ctx)
	fmt.Fprintf(&b, "上传凭据：err=%v keys=%d\n", ierr, len(info))
	for k, v := range info {
		if len(v) > 60 {
			v = v[:60] + "…"
		}
		fmt.Fprintf(&b, "  %s=%s\n", k, v)
	}

	if uk, e := target.UploadKey(ctx); e != nil {
		fmt.Fprintf(&b, "UploadKey err=%v\n", e)
	} else {
		fmt.Fprintf(&b, "UploadKey=%s (len=%d)\n", uk, len(uk))
	}

	signNeeded := ""
	fetcher := func(signCheck string) ([]byte, error) {
		signNeeded = signCheck
		return probeRange(ctx, source, pickcode, signCheck)
	}
	start := time.Now()
	pick, err := target.RapidUpload(ctx, cid, name, sha1, size, fetcher)
	fmt.Fprintf(&b, "RapidUpload：err=%v 耗时=%s 校验区间=%q pickcode=%s\n",
		err, time.Since(start).Round(time.Millisecond), signNeeded, pick)
	if err != nil {
		return b.String()
	}
	id, _, derr := target.PickcodeOf(ctx, cid, name)
	fmt.Fprintf(&b, "目录里：id=%s err=%v\n", id, derr)
	return b.String()
}

func probeRange(ctx context.Context, c *p115.Client, pickcode, signCheck string) ([]byte, error) {
	parts := strings.SplitN(signCheck, "-", 2)
	if len(parts) != 2 {
		return nil, fmt.Errorf("区间格式不对: %s", signCheck)
	}
	start, _ := strconv.ParseInt(strings.TrimSpace(parts[0]), 10, 64)
	end, _ := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
	link, header, err := c.DownloadURL(ctx, pickcode, "", 5*time.Minute)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, link, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", p115.UserAgent)
	req.Header.Set("Referer", "https://115.com/")
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", start, end))
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := (&http.Client{Timeout: 60 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	fmt.Printf("  取校验块 %s → HTTP %d，%d 字节\n", signCheck, resp.StatusCode, len(data))
	return data, err
}

// probeUpload 用真实账号试探上传初始化接口的现状（不同路径 / 不同编码）。
func probeUpload(ctx context.Context, cookies, uid, sha1, name string, size int64, cid string) string {
	var b strings.Builder
	target := "U_1_" + cid
	uas := []string{
		p115.UserAgent,
		"Mozilla/5.0 115disk/36.2.28 115Browser/36.2.28 115wangpan_android/36.2.28",
	}
	urls := []string{
		"https://uplb.115.com/4.0/initupload.php",
		"https://uplb.115.com/3.0/initupload.php",
		"https://uplb.115.com/4.0/initupload",
	}
	forms := []struct {
		label string
		form  map[string]string
	}{
		{"plain(fileid/target)", map[string]string{
			"appid": "0", "userid": uid, "fileid": sha1, "filename": name,
			"filesize": strconv.FormatInt(size, 10), "target": target,
			"sign_key": "", "sign_check": "", "topupload": "true", "appversion": "36.2.28",
		}},
		{"plain(sha1/t)", map[string]string{
			"userid": uid, "sha1": sha1, "filename": name,
			"filesize": strconv.FormatInt(size, 10), "target": target,
		}},
	}
	cl := &http.Client{Timeout: 30 * time.Second}
	for _, u := range urls {
		for _, ua := range uas {
			for _, f := range forms {
				vals := url.Values{}
				for k, v := range f.form {
					vals.Set(k, v)
				}
				req, _ := http.NewRequestWithContext(ctx, http.MethodPost, u, strings.NewReader(vals.Encode()))
				req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
				req.Header.Set("User-Agent", ua)
				req.Header.Set("Cookie", cookies)
				resp, err := cl.Do(req)
				if err != nil {
					fmt.Fprintf(&b, "%s | %s | %s → err=%v\n", u, ua[:22], f.label, err)
					continue
				}
				raw, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
				resp.Body.Close()
				fmt.Fprintf(&b, "%s | %s | %s → %d  %q\n", u, ua[:22], f.label, resp.StatusCode, string(raw))
			}
		}
	}
	return b.String()
}
