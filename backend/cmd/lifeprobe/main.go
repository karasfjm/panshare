// lifeprobe 用真实账号验证「115 生活事件」接口，排事件监控不工作时用。
//
// 用法（容器里跑，容器内已有 /config）：
//
//	lifeprobe -db /config/panshare.db -minutes 60 -n 20
//
// 只打印事件条数与几个字段，不打印 cookie。
package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"os"
	"time"

	_ "modernc.org/sqlite"

	"panshare/internal/p115"
)

func main() {
	db := flag.String("db", "/config/panshare.db", "sqlite 路径")
	minutes := flag.Int("minutes", 60, "往回看多少分钟")
	n := flag.Int("n", 20, "最多看几条")
	accIdx := flag.Int("acc", 0, "第几个账号")
	debug := flag.Bool("debug", false, "逐个试探事件接口的真实路径")
	mkdirUnder := flag.String("mkdir", "", "在该目录 cid 下建一个测试目录")
	mkdirName := flag.String("name", "", "测试目录名")
	rmID := flag.String("rm", "", "删掉这个 cid（测试目录收尾用）")
	rmPid := flag.String("pid", "", "收尾删除时的父目录 cid")
	copyID := flag.String("copy", "", "复制这个 file_id")
	copyTo := flag.String("to", "", "复制到哪个目录 cid")
	lsID := flag.String("ls", "", "列这个目录 cid")
	delID := flag.String("deldebug", "", "试探删除参数：要删的 id")
	delPid := flag.String("delpid", "", "试探删除参数：父目录 cid")
	pathID := flag.String("path", "", "试探这个 cid 的路径取法")
	dump := flag.Bool("dump", false, "原样打印事件响应")
	flag.Parse()

	conn, err := sql.Open("sqlite", *db+"?mode=ro")
	if err != nil {
		fmt.Println("打开数据库失败:", err)
		os.Exit(1)
	}
	defer conn.Close()
	rows, err := conn.Query(`SELECT name, cookies FROM accounts ORDER BY created_at ASC`)
	if err != nil {
		fmt.Println("读账号失败:", err)
		os.Exit(1)
	}
	type acc struct{ name, cookies string }
	var accs []acc
	for rows.Next() {
		var a acc
		if err := rows.Scan(&a.name, &a.cookies); err == nil && a.cookies != "" {
			accs = append(accs, a)
		}
	}
	rows.Close()
	if len(accs) == 0 {
		fmt.Println("没有可用账号")
		os.Exit(1)
	}
	if *accIdx < 0 || *accIdx >= len(accs) {
		*accIdx = 0
	}
	a := accs[*accIdx]
	c := p115.New(a.cookies)
	c.SetQPS(1)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	fmt.Printf("账号 %s uid=%q\n", a.name, c.UID(ctx))

	if *mkdirUnder != "" {
		name := *mkdirName
		if name == "" {
			name = "PSTEST-" + time.Now().Format("150405")
		}
		id, err := c.Mkdir(ctx, *mkdirUnder, name)
		fmt.Printf("创建目录 %q → cid=%s err=%v\n", name, id, err)
		return
	}
	if *rmID != "" {
		err := c.DeleteIn(ctx, []string{*rmID}, *rmPid)
		fmt.Printf("删除 %s (pid=%s) err=%v\n", *rmID, *rmPid, err)
		return
	}
	if *copyID != "" {
		err := c.Copy(ctx, []string{*copyID}, *copyTo)
		fmt.Printf("复制 %s → %s err=%v\n", *copyID, *copyTo, err)
		return
	}
	if *delID != "" {
		fmt.Println(c.DebugDelete(ctx, *delID, *delPid))
		fmt.Println(c.DebugDelete2(ctx, *delID, *delPid))
		return
	}
	if *lsID != "" {
		nodes, err := c.ListDir(ctx, *lsID)
		fmt.Printf("列目录 %s err=%v\n", *lsID, err)
		for _, n := range nodes {
			fmt.Printf("  id=%s dir=%v name=%q size=%d\n", n.ID, n.IsDir, n.Name, n.Size)
		}
		return
	}
	if *pathID != "" {
		fmt.Println(c.DebugPath(ctx, *pathID))
		if n, err := c.Details(ctx, *pathID); err == nil {
			fmt.Printf("Details: id=%s parent=%s name=%q isdir=%v\n", n.ID, n.ParentID, n.Name, n.IsDir)
		}
		if p, err := c.DirPath(ctx, *pathID); err == nil {
			fmt.Printf("DirPath=%q\n", p)
		}
		return
	}
	if *dump {
		fmt.Println(c.DebugLifeRaw(ctx, 2))
		return
	}
	if *debug {
		fmt.Println(c.DebugLife(ctx, 5))
		return
	}
	from := time.Now().Unix() - int64(*minutes)*60
	evs, err := c.LifeEvents(ctx, from, 0, *n)
	if err != nil {
		fmt.Println("拉事件失败:", err)
		os.Exit(2)
	}
	fmt.Printf("拉到 %d 条事件（最近 %d 分钟）\n", len(evs), *minutes)
	for i, e := range evs {
		if i >= *n {
			break
		}
		p, _ := c.DirPath(ctx, e.ParentID)
		fmt.Printf("  #%d t=%d type=%d id=%d dir=%q name=%q dirpath=%q pick=%v size=%d toolong=%v\n",
			i+1, e.Time, e.Type, e.ID, e.ParentID, e.Name, p, e.Pickcode != "", e.Size, e.IsDir)
	}
}
