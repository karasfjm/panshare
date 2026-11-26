package service

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"io"
	"log"
	"path"
	"sort"
	"sync"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
	"panshare/internal/strm"
)

// 新生成引擎：115 列目录受全局 QPS 限制，仍然串行；写 strm、算直链、
// 下载元数据这些本地/网络混合操作交给 worker pool 并发做，索引攒批落库。
// 行为上对齐 QBP115StrmHelper 的覆盖 / 过滤 / 清理安全策略。

type strmJob struct {
	node    p115.Node
	prefix  string
	relPath string
	strmRel string
	metas   []p115.Node
}

type strmResult struct {
	job      strmJob
	pickcode string
	abs      string
	changed  bool
	skipped  bool // overwrite_mode=never 命中已存在的 strm
	err      error
}

func (a *App) workerCount(c *model.StrmConfig) int {
	n := c.Concurrency
	if n <= 0 {
		n = a.Settings().DefaultConcurrency
	}
	if n <= 0 {
		n = 8
	}
	if n > 32 {
		n = 32 // 再多只会把 115 接口和磁盘打满
	}
	return n
}

// runStrm 是实际使用的生成入口。
func (a *App) runStrm(c *model.StrmConfig, mode GenMode) {
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Hour)
	defer cancel()

	start := time.Now()
	st := &genStats{}
	modeText := map[GenMode]string{ModeInc: "增量生成", ModeFull: "全量生成", ModeRebuild: "全新生成"}[mode]
	a.Event("info", "strm", fmt.Sprintf("%s开始：%s", modeText, c.Name), "")

	client, err := a.client(c.AccountID)
	if err != nil {
		a.genFail(c, modeText, err)
		return
	}
	uid := client.UID(ctx)
	if uid == "" {
		a.genFail(c, modeText, fmt.Errorf("读不到 115 用户 ID，请到「115 助手」检查账号"))
		return
	}

	oldRows := a.DB.ListFiles(c.ID)
	if mode == ModeRebuild {
		for _, row := range oldRows {
			if row.StrmPath == "" {
				continue
			}
			if _, err := a.Store.RemoveAbs(row.StrmPath, true); err == nil {
				st.Cleared++
			}
		}
		_ = a.DB.ClearFiles(c.ID)
		oldRows = nil
	}

	f := a.strmFilter(c)
	metaExts := a.metaExts(c)
	force := mode != ModeInc
	seen := map[string]bool{}
	taken := map[string]bool{}
	sig := &sigSet{}
	rows := make([]model.StrmFile, 0, 256)

	jobs := make(chan strmJob, 64)
	results := make(chan strmResult, 64)
	var wg sync.WaitGroup
	for i := 0; i < a.workerCount(c); i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				if ctx.Err() != nil {
					return
				}
				results <- a.processStrmJob(ctx, client, c, uid, job, metaExts)
			}
		}()
	}
	go func() {
		wg.Wait()
		close(results)
	}()

	var walk func(cid, prefix, rel string, depth int) error
	walk = func(cid, prefix, rel string, depth int) error {
		if depth > 64 {
			st.Failed++
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		nodes, err := client.ListDir(ctx, cid)
		if err != nil {
			st.Failed++
			a.Event("warn", "strm", fmt.Sprintf("%s：列目录失败 %s（%v）", c.Name, rel, err), "")
			return nil
		}
		st.Dirs++
		metas := map[string][]p115.Node{}
		for _, n := range nodes {
			if !n.IsDir && matchMeta(n.Name, metaExts) {
				metas[strm.Stem(n.Name)] = append(metas[strm.Stem(n.Name)], n)
			}
		}
		for _, n := range nodes {
			name := strm.Sanitize(n.Name, 200)
			relPath := path.Join(rel, name)
			if n.IsDir {
				if f.skipDir(n.Name) {
					continue
				}
				if err := walk(n.ID, prefix, relPath, depth+1); err != nil {
					return err
				}
				continue
			}
			st.Scanned++
			if !f.allow(n.Name, n.Size) {
				st.Filtered++
				continue
			}
			sig.add(path.Join(prefix, relPath), n)
			jobs <- strmJob{
				node: n, prefix: prefix, relPath: relPath,
				strmRel: strmRelFor(prefix, relPath, n, taken),
				metas:   metas[strm.Stem(n.Name)],
			}
		}
		return nil
	}

	var walkErr error
	for _, p := range c.EffectivePaths() {
		rootCID := firstNonEmptyStr(p.RemoteID, "0")
		prefix := a.Store.CleanRel(p.LocalDir)
		if err := walk(rootCID, prefix, "", 0); err != nil {
			walkErr = err
			break
		}
	}
	close(jobs)

	for res := range results {
		if res.err != nil {
			st.Failed++
			a.Event("warn", "strm", fmt.Sprintf("%s：写入失败 %s（%v）", c.Name, res.job.relPath, res.err), "")
			continue
		}
		if res.skipped {
			st.Skipped++
			continue
		}
		if res.changed {
			st.Written++
		} else {
			st.Skipped++
		}
		seen[res.job.node.ID] = true
		n := res.job.node
		rows = append(rows, model.StrmFile{
			ConfigID: c.ID, AccountID: c.AccountID, FileID: n.ID, Pickcode: res.pickcode,
			Name: n.Name, RelPath: res.job.relPath, StrmPath: res.abs, Size: n.Size,
			Sha1: n.Sha1, ParentID: n.ParentID, Source: "strm",
		})
	}

	if walkErr != nil && walkErr != context.Canceled {
		a.genFail(c, modeText, walkErr)
		return
	}
	if err := a.DB.UpsertFiles(rows); err != nil {
		log.Printf("批量写索引失败: %v", err)
	}

	cleanupSig, cleanupCount, cleanupNote := "", 0, ""
	if force && st.Failed == 0 && len(oldRows) > 0 {
		cleanupSig, cleanupCount, cleanupNote, st.Removed = a.cleanupStale(c, oldRows, seen)
	}

	cost := time.Since(start).Seconds()
	msg := fmt.Sprintf("%s完成：%s · %d 个文件 · 写入 %d（跳过 %d）",
		modeText, c.Name, len(seen), st.Written, st.Skipped)
	if st.Filtered > 0 {
		msg += fmt.Sprintf(" · 过滤 %d", st.Filtered)
	}
	if st.Cleared > 0 {
		msg += fmt.Sprintf(" · 清理旧文件 %d", st.Cleared)
	}
	if st.Removed > 0 {
		msg += fmt.Sprintf(" · 移除残留 %d", st.Removed)
	}
	if cleanupNote != "" {
		msg += " · " + cleanupNote
	}
	if st.Failed > 0 {
		msg += fmt.Sprintf(" · 失败 %d", st.Failed)
	}
	msg += fmt.Sprintf(" · 用时 %.1fs", cost)

	status := "idle"
	level := "success"
	if st.Failed > 0 {
		status, level = "warn", "warn"
	}
	_ = a.DB.UpdateConfig(c.ID, map[string]any{
		"last_status": status, "last_run_at": time.Now().Unix(),
		"last_message": msg, "files": len(seen), "last_sig": sig.sum(),
		"cleanup_stale_sig": cleanupSig, "cleanup_stale_count": cleanupCount,
	})
	a.Event(level, "strm", msg, fmt.Sprintf(`{"config":"%s","mode":"%s","files":%d,"written":%d,"skipped":%d,"failed":%d}`,
		c.ID, mode, len(seen), st.Written, st.Skipped, st.Failed))
}

func (a *App) processStrmJob(ctx context.Context, client *p115.Client, c *model.StrmConfig, uid string, job strmJob, metaExts map[string]bool) strmResult {
	res := strmResult{job: job}
	pickcode := job.node.Pickcode
	if pickcode == "" {
		if d, err := client.Details(ctx, job.node.ID); err == nil {
			pickcode = d.Pickcode
		}
	}
	if pickcode == "" {
		res.err = fmt.Errorf("没有 pickcode")
		return res
	}
	res.pickcode = pickcode
	if c.OverwriteMode == "never" && a.strmExists(job.strmRel) {
		res.skipped = true
		return res
	}
	abs, changed, err := a.Store.Write(job.strmRel, a.PlayURLWith(c.StrmBase, pickcode, job.node.Name, uid))
	if err != nil {
		res.err = err
		return res
	}
	res.abs, res.changed = abs, changed
	if c.DownloadMeta || a.Settings().DownloadMeta {
		a.saveMeta(ctx, client, c, job.prefix, path.Dir(job.relPath), job.metas)
	}
	return res
}

// staleSignature 把残留集合压成一个短指纹，用来判断「同一批残留是否连续出现」。
func staleSignature(rows []model.StrmFile) string {
	if len(rows) == 0 {
		return ""
	}
	ids := make([]string, 0, len(rows))
	for _, r := range rows {
		ids = append(ids, r.FileID)
	}
	sort.Strings(ids)
	h := sha1.New()
	for _, id := range ids {
		_, _ = io.WriteString(h, id)
		_, _ = io.WriteString(h, "\n")
	}
	return hex.EncodeToString(h.Sum(nil))[:16]
}

// cleanupStale 实现三道安全门：连续 N 轮稳定、删除比例阈值、人工二次确认。
// 返回 (残留指纹, 稳定轮数, 说明, 实际删除数)。
func (a *App) cleanupStale(c *model.StrmConfig, oldRows []model.StrmFile, seen map[string]bool) (string, int, string, int) {
	stale := make([]model.StrmFile, 0, 16)
	for _, row := range oldRows {
		if row.FileID != "" && !seen[row.FileID] {
			stale = append(stale, row)
		}
	}
	sig := staleSignature(stale)
	if sig == "" {
		return "", 0, "", 0
	}

	s := a.Settings()
	rounds := s.CleanupStableRounds
	if rounds <= 0 {
		rounds = 3
	}
	count := 1
	if sig == c.CleanupStaleSig {
		count = c.CleanupStaleCount + 1
	}
	if count < rounds {
		return sig, count, fmt.Sprintf("残留 %d 个，稳定 %d/%d 轮后清理", len(stale), count, rounds), 0
	}

	if s.CleanupMaxDeleteRatio > 0 && len(stale)*100 > len(oldRows)*s.CleanupMaxDeleteRatio {
		note := fmt.Sprintf("残留 %d/%d 超过阈值 %d%%，已跳过清理", len(stale), len(oldRows), s.CleanupMaxDeleteRatio)
		a.Event("warn", "strm", c.Name+" · "+note, "")
		return sig, 0, note, 0
	}

	if s.CleanupConfirmRequired {
		pending := a.DB.ListCleanups("pending")
		for _, p := range pending {
			if p.ConfigID == c.ID && p.StaleSig == sig {
				return sig, count, "清理计划待确认", 0
			}
		}
		items := make([]model.CleanupItem, 0, len(stale))
		for _, r := range stale {
			items = append(items, model.CleanupItem{FileID: r.FileID, Path: r.StrmPath, Name: r.Name})
		}
		ttl := s.CleanupConfirmTTLHours
		if ttl <= 0 {
			ttl = 24
		}
		x := model.CleanupConfirmation{
			ID: fmt.Sprint(time.Now().UnixNano()), ConfigID: c.ID, ConfigName: c.Name,
			StaleSig: sig, Items: items, Total: len(stale),
			Ratio: s.CleanupMaxDeleteRatio, State: "pending",
			CreatedAt: time.Now().Unix(), ExpiresAt: time.Now().Add(time.Duration(ttl) * time.Hour).Unix(),
		}
		if err := a.DB.CreateCleanup(x); err == nil {
			shortID := x.ID
			if len(shortID) > 10 {
				shortID = shortID[:10]
			}
			note := fmt.Sprintf("残留 %d 个待确认清理（ID %s，%d 小时内有效）", len(stale), shortID, ttl)
			a.Event("info", "strm", c.Name+" · "+note, "")
			return sig, count, note, 0
		}
	}

	ids := make([]string, 0, len(stale))
	removed := 0
	withMeta := s.DeleteMetaWithStrm
	for _, r := range stale {
		if r.StrmPath != "" {
			if _, err := a.Store.RemoveAbs(r.StrmPath, withMeta); err == nil {
				removed++
			}
		}
		ids = append(ids, r.FileID)
	}
	if err := a.DB.DeleteFileRows(c.ID, ids); err != nil {
		log.Printf("删除残留索引失败: %v", err)
	}
	return "", 0, "", removed
}

// ---------------------------------------------------------------- 确认队列 API

func (a *App) CleanupQueue() []model.CleanupConfirmation {
	a.DB.ExpireCleanups()
	return a.DB.ListCleanups("pending")
}

func (a *App) ApproveCleanup(id string) (int, error) {
	x, err := a.DB.GetCleanup(id)
	if err != nil || x == nil {
		return 0, fmt.Errorf("清理计划不存在")
	}
	if x.State != "pending" {
		return 0, fmt.Errorf("清理计划已处理")
	}
	if x.ExpiresAt > 0 && time.Now().Unix() > x.ExpiresAt {
		_ = a.DB.SetCleanupState(id, "expired")
		return 0, fmt.Errorf("清理计划已过期")
	}
	ids := make([]string, 0, len(x.Items))
	removed := 0
	withMeta := a.Settings().DeleteMetaWithStrm
	for _, it := range x.Items {
		if it.Path != "" {
			if _, err := a.Store.RemoveAbs(it.Path, withMeta); err == nil {
				removed++
			}
		}
		ids = append(ids, it.FileID)
	}
	if err := a.DB.DeleteFileRows(x.ConfigID, ids); err != nil {
		return removed, err
	}
	_ = a.DB.SetCleanupState(id, "approved")
	_ = a.DB.UpdateConfig(x.ConfigID, map[string]any{"cleanup_stale_sig": "", "cleanup_stale_count": 0})
	a.Event("success", "strm", fmt.Sprintf("%s · 清理确认通过，移除 %d 个残留", x.ConfigName, removed), "")
	return removed, nil
}

func (a *App) CancelCleanup(id string) error {
	x, err := a.DB.GetCleanup(id)
	if err != nil || x == nil {
		return fmt.Errorf("清理计划不存在")
	}
	if x.State != "pending" {
		return fmt.Errorf("清理计划已处理")
	}
	return a.DB.SetCleanupState(id, "cancelled")
}
