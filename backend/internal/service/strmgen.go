package service

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
	"panshare/internal/strm"
)

// GenMode 三种生成语义：
//
//	inc     增量：只补新增，不动已有 strm
//	full    全量：清索引重扫，strm 全部重新写一遍（内容没变也会跳过写盘）
//	rebuild 全新：先删掉这条配置生成过的旧 strm（只碰自己的），再从头生成
type GenMode string

const (
	ModeInc     GenMode = "inc"
	ModeFull    GenMode = "full"
	ModeRebuild GenMode = "rebuild"
)

type genStats struct {
	Dirs     int
	Scanned  int
	Written  int
	Skipped  int
	Filtered int
	Failed   int
	Cleared  int
	Removed  int
	Seen     int
}

// RunStrm 异步跑一次生成。返回是否受理。
func (a *App) RunStrm(id string, mode GenMode) (bool, error) {
	c, err := a.DB.GetConfig(id)
	if err != nil {
		return false, err
	}
	if c == nil {
		return false, fmt.Errorf("配置不存在")
	}
	if mode != ModeInc && mode != ModeFull && mode != ModeRebuild {
		mode = ModeInc
	}
	if !a.tryBegin(id) {
		return false, fmt.Errorf("这条配置正在生成，请稍候")
	}

	_ = a.DB.UpdateConfig(id, map[string]any{"last_status": "running"})
	go func() {
		defer a.endRun(id)
		a.runStrm(c, mode)
	}()
	return true, nil
}

// tryBegin 抢占这条配置的运行位，成功返回 true。
func (a *App) tryBegin(id string) bool {
	a.genMu.Lock()
	defer a.genMu.Unlock()
	if a.running[id] {
		return false
	}
	a.running[id] = true
	return true
}

func (a *App) endRun(id string) {
	a.genMu.Lock()
	delete(a.running, id)
	a.genMu.Unlock()
}

// IsRunning 给列表接口标注运行状态。
func (a *App) IsRunning(id string) bool {
	a.genMu.Lock()
	defer a.genMu.Unlock()
	return a.running[id]
}

// ---------------------------------------------------------------- 目录指纹

// sigSet 收集「相对路径 + 文件 id + 大小 + sha1」，排序后哈希成一个短串。
// 事件监控靠它判断目录到底有没有变化：不变就不跑生成，避免空转。
type sigSet struct{ parts []string }

func (s *sigSet) add(relPath string, n p115.Node) {
	s.parts = append(s.parts, relPath+"|"+n.ID+"|"+fmt.Sprint(n.Size)+"|"+n.Sha1)
}

func (s *sigSet) sum() string {
	if len(s.parts) == 0 {
		return "empty"
	}
	sort.Strings(s.parts)
	h := sha1.New()
	for _, p := range s.parts {
		_, _ = io.WriteString(h, p)
		_, _ = io.WriteString(h, "\n")
	}
	return hex.EncodeToString(h.Sum(nil))[:16]
}

// StrmSignature 只列目录算指纹，不写任何 strm，用来替代「到点就跑一遍」。
func (a *App) StrmSignature(c *model.StrmConfig) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	client, err := a.client(c.AccountID)
	if err != nil {
		return "", err
	}
	f := a.strmFilter(c)
	sig := &sigSet{}
	var failed int
	var walk func(cid, prefix, rel string, depth int) error
	walk = func(cid, prefix, rel string, depth int) error {
		if depth > 64 {
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		nodes, err := client.ListDir(ctx, cid)
		if err != nil {
			// 列目录失败不能当成「没变化」，否则会把监控永久静音
			failed++
			return nil
		}
		for _, n := range nodes {
			relPath := path.Join(rel, strm.Sanitize(n.Name, 200))
			if n.IsDir {
				if f.skipDir(n.Name) {
					continue
				}
				if err := walk(n.ID, prefix, relPath, depth+1); err != nil {
					return err
				}
				continue
			}
			if !f.allow(n.Name, n.Size) {
				continue
			}
			sig.add(path.Join(prefix, relPath), n)
		}
		return nil
	}
	for _, p := range c.EffectivePaths() {
		if err := walk(firstNonEmptyStr(p.RemoteID, "0"), a.Store.CleanRel(p.LocalDir), "", 0); err != nil {
			return "", err
		}
	}
	if failed > 0 && len(sig.parts) == 0 {
		return "", fmt.Errorf("%d 个目录列不出来", failed)
	}
	return sig.sum(), nil
}

// strmFilter 是这条配置的生成过滤条件。对应 QBP115StrmHelper 的扩展名、
// 黑名单关键词、最小文件大小与待整理路径跳过。
type strmFilter struct {
	exts        map[string]bool
	allFiles    bool
	keywords    []string
	minBytes    int64
	skipPending bool
	pendingDirs []string
}

func splitList(raw string) []string {
	raw = strings.ReplaceAll(raw, "，", ",")
	raw = strings.ReplaceAll(raw, "\n", ",")
	out := make([]string, 0, 4)
	for _, p := range strings.Split(raw, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func (a *App) strmFilter(c *model.StrmConfig) strmFilter {
	f := strmFilter{
		exts:        a.strmExts(c),
		allFiles:    c.AllFiles,
		keywords:    splitList(c.ExcludeKeywords),
		minBytes:    int64(c.MinFileSizeMB) << 20,
		skipPending: c.SkipPending,
		pendingDirs: splitList(c.PendingDirs),
	}
	if len(f.pendingDirs) == 0 {
		f.pendingDirs = []string{"待整理", "待归类", "待整理区"}
	}
	return f
}

// skipDir 只用于中间目录；映射根本身不做判断。
func (f strmFilter) skipDir(name string) bool {
	if !f.skipPending {
		return false
	}
	for _, kw := range f.pendingDirs {
		if strings.Contains(name, kw) {
			return true
		}
	}
	return false
}

func (f strmFilter) allow(name string, size int64) bool {
	if !strm.MatchExt(name, f.exts, f.allFiles) {
		return false
	}
	lower := strings.ToLower(name)
	for _, kw := range f.keywords {
		if strings.Contains(lower, strings.ToLower(kw)) {
			return false
		}
	}
	if f.minBytes > 0 && size < f.minBytes {
		return false
	}
	return true
}

// strmExists 判断目标 strm 是否已经落盘，用于 overwrite_mode=never。
func (a *App) strmExists(rel string) bool {
	abs, err := a.Store.Abs(rel)
	if err != nil {
		return false
	}
	if !strings.HasSuffix(strings.ToLower(abs), ".strm") {
		abs += ".strm"
	}
	_, err = os.Stat(abs)
	return err == nil
}

// strmExts 取这条配置要生成 strm 的扩展名集合。
func (a *App) strmExts(c *model.StrmConfig) map[string]bool {
	if raw := strings.TrimSpace(c.IncludeExts); raw != "" {
		return strm.ParseExts(raw)
	}
	return strm.ParseExts(a.Settings().DefaultExts)
}

// metaExts 取这条配置要下载的元数据扩展名集合。
func (a *App) metaExts(c *model.StrmConfig) map[string]bool {
	if raw := strings.TrimSpace(c.MetaExts); raw != "" {
		return strm.ParseExts(raw)
	}
	if raw := strings.TrimSpace(a.Settings().DefaultMetaExts); raw != "" {
		return strm.ParseExts(raw)
	}
	return nil // nil = 用内置的 IsMeta 判断
}

// strmRelFor 算 strm 的相对路径；同目录 a.mkv / a.mp4 会撞到同一个 a.strm，
// 用文件 id 后缀区分。
func strmRelFor(prefix, relPath string, n p115.Node, taken map[string]bool) string {
	rel := path.Join(prefix, relPath)
	if !strings.HasSuffix(strings.ToLower(rel), ".strm") {
		rel += ".strm"
	}
	if taken != nil && taken[rel] {
		base := strings.TrimSuffix(rel, ".strm")
		suffix := n.ID
		if len(suffix) > 6 {
			suffix = suffix[len(suffix)-6:]
		}
		rel = base + " [" + suffix + "].strm"
	}
	if taken != nil {
		taken[rel] = true
	}
	return rel
}

// writeStrmFor 给单个文件写 strm + 落索引，供事件监控按事件逐步处理。
func (a *App) writeStrmFor(c *model.StrmConfig, prefix, relPath string, n p115.Node, uid string, metaNodes []p115.Node) (string, bool, error) {
	pickcode := n.Pickcode
	if pickcode == "" {
		return "", false, fmt.Errorf("没有 pickcode")
	}
	rel := strmRelFor(prefix, relPath, n, nil)
	if c.OverwriteMode == "never" && a.strmExists(rel) {
		return "", false, nil
	}
	abs, changed, err := a.Store.Write(rel, a.PlayURLWith(c.StrmBase, pickcode, n.Name, uid))
	if err != nil {
		return "", false, err
	}
	// 同名文件被重新上传/复制时 file_id 会变，先清掉这条 strm 上的旧索引行
	if err := a.DB.DeleteFileRowsByStrmPath(c.ID, abs); err != nil {
		log.Printf("清理同名旧索引失败: %v", err)
	}
	row := model.StrmFile{
		ConfigID: c.ID, AccountID: c.AccountID, FileID: n.ID, Pickcode: pickcode,
		Name: n.Name, RelPath: relPath, StrmPath: abs, Size: n.Size,
		Sha1: n.Sha1, ParentID: n.ParentID, Source: "strm",
	}
	if err := a.DB.UpsertFile(row); err != nil {
		log.Printf("写索引失败: %v", err)
	}
	if c.DownloadMeta || a.Settings().DownloadMeta {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		if client, err := a.client(c.AccountID); err == nil {
			a.saveMeta(ctx, client, c, prefix, path.Dir(relPath), metaNodes)
		}
	}
	return abs, changed, nil
}

// runStrmLegacy 是改造前的单 goroutine 版本，保留作对照，不再被调用。
func (a *App) runStrmLegacy(c *model.StrmConfig, mode GenMode) {
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
		// 只删这条配置索引里记录过的 strm，绝不按目录整片删
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

	exts := a.strmExts(c)
	metaExts := a.metaExts(c)
	force := mode != ModeInc
	seen := map[string]bool{}
	taken := map[string]bool{}
	sig := &sigSet{}

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
		// 同一目录里的元数据文件先按「主文件名」归组，视频生成完再一起下
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
				if err := walk(n.ID, prefix, relPath, depth+1); err != nil {
					return err
				}
				continue
			}
			st.Scanned++
			if !strm.MatchExt(n.Name, exts, c.AllFiles) {
				st.Filtered++
				continue
			}
			// 顺路记指纹：跑完写回库，事件监控下次比对就知道没变化
			sig.add(path.Join(prefix, relPath), n)
			pickcode := n.Pickcode
			if pickcode == "" {
				if d, err := client.Details(ctx, n.ID); err == nil {
					pickcode = d.Pickcode
				}
			}
			if pickcode == "" {
				st.Failed++
				continue
			}
			strmRel := strmRelFor(prefix, relPath, n, taken)

			abs, changed, err := a.Store.Write(strmRel, a.PlayURLWith(c.StrmBase, pickcode, n.Name, uid))
			if err != nil {
				st.Failed++
				a.Event("warn", "strm", fmt.Sprintf("%s：写入失败 %s（%v）", c.Name, relPath, err), "")
				continue
			}
			if changed {
				st.Written++
			} else {
				st.Skipped++
			}
			seen[n.ID] = true
			row := model.StrmFile{
				ConfigID: c.ID, AccountID: c.AccountID, FileID: n.ID, Pickcode: pickcode,
				Name: n.Name, RelPath: relPath, StrmPath: abs, Size: n.Size,
				Sha1: n.Sha1, ParentID: n.ParentID, Source: "strm",
			}
			if err := a.DB.UpsertFile(row); err != nil {
				log.Printf("写索引失败: %v", err)
			}
			if c.DownloadMeta || a.Settings().DownloadMeta {
				a.saveMeta(ctx, client, c, prefix, rel, metas[strm.Stem(n.Name)])
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
	if walkErr != nil && walkErr != context.Canceled {
		a.genFail(c, modeText, walkErr)
		return
	}

	// 全量 / 全新：云端已删的文件，本地残留一起收掉
	if force && st.Failed == 0 && oldRows != nil {
		for _, row := range oldRows {
			if seen[row.FileID] {
				continue
			}
			if row.StrmPath != "" {
				if _, err := a.Store.RemoveAbs(row.StrmPath, true); err == nil {
					st.Removed++
				}
			}
			_ = a.DB.DeleteFileRow(row.ID)
		}
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
	if st.Failed > 0 {
		msg += fmt.Sprintf(" · 失败 %d", st.Failed)
	}
	msg += fmt.Sprintf(" · 用时 %.1fs", cost)

	status := "idle"
	if st.Failed > 0 {
		status = "warn"
	}
	_ = a.DB.UpdateConfig(c.ID, map[string]any{
		"last_status": status, "last_run_at": time.Now().Unix(),
		"last_message": msg, "files": len(seen), "last_sig": sig.sum(),
	})
	level := "success"
	if st.Failed > 0 {
		level = "warn"
	}
	a.Event(level, "strm", msg, fmt.Sprintf(`{"config":"%s","mode":"%s","files":%d,"written":%d,"skipped":%d,"failed":%d}`,
		c.ID, mode, len(seen), st.Written, st.Skipped, st.Failed))
}

func (a *App) genFail(c *model.StrmConfig, modeText string, err error) {
	msg := fmt.Sprintf("%s失败：%s · %v", modeText, c.Name, err)
	_ = a.DB.UpdateConfig(c.ID, map[string]any{
		"last_status": "error", "last_run_at": time.Now().Unix(), "last_message": msg,
	})
	a.Event("error", "strm", msg, "")
}

// matchMeta 判断一个文件是否属于这条配置要下载的元数据。
func matchMeta(name string, exts map[string]bool) bool {
	if len(exts) == 0 {
		return strm.IsMeta(name)
	}
	return exts[strm.Ext(name)]
}

// saveMeta 下载同名的 nfo / 字幕 / 海报。
func (a *App) saveMeta(ctx context.Context, client *p115.Client, c *model.StrmConfig, prefix, relDir string, nodes []p115.Node) {
	if len(nodes) == 0 {
		return
	}
	exts := a.metaExts(c)
	for _, n := range nodes {
		if !matchMeta(n.Name, exts) {
			continue
		}
		if n.Size > 50<<20 {
			continue
		}
		dest, err := a.Store.Abs(path.Join(prefix, relDir, strm.Sanitize(n.Name, 200)))
		if err != nil {
			continue
		}
		if fi, err := os.Stat(dest); err == nil && fi.Size() == n.Size && n.Size > 0 {
			continue
		}
		if err := a.downloadNode(ctx, client, n, dest); err != nil {
			log.Printf("下载元数据 %s 失败: %v", n.Name, err)
		}
	}
}

// downloadNode 把一个 115 文件落到本地（元数据文件、小体积附件）。
func (a *App) downloadNode(ctx context.Context, client *p115.Client, n p115.Node, dest string) error {
	pickcode := n.Pickcode
	if pickcode == "" {
		if d, err := client.Details(ctx, n.ID); err == nil {
			pickcode = d.Pickcode
		}
	}
	if pickcode == "" {
		return fmt.Errorf("没有 pickcode")
	}
	link, header, err := client.DownloadURL(ctx, pickcode, "", 5*time.Minute)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, link, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", p115.UserAgent)
	req.Header.Set("Referer", "https://115.com/")
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := (&http.Client{Timeout: 2 * time.Minute}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	tmp := dest + ".part"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, io.LimitReader(resp.Body, 200<<20)); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	f.Close()
	return os.Rename(tmp, dest)
}
