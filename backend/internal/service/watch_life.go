package service

// 事件监控：靠 115 的「生活事件」接口做实时增量。
//
// 以前是「到点扫一遍目录指纹」，目录一大就要把整棵树列一遍，115 那边也吃不住。
// 现在改成：按账号拉一段操作事件（谁被加了、谁被删了、谁改了名/搬了家），
// 只处理落在这条配置监控目录里的那些文件，一个事件写一个 strm。
// 事件接口不可用（被限流 / 改接口）时退回目录指纹探测，功能不会瞎掉。

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"path"
	"strings"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
	"panshare/internal/strm"
)

type lifeCursor struct {
	Time int64 `json:"time"`
	ID   int64 `json:"id"`
}

func (a *App) cursorKey(accID string) string { return "life_cursor:" + accID }

// loadLifeCursor 读游标；没有就给「5 分钟前」当起点，
// 避免第一次启用监控就把几万条历史事件全跑一遍。
func (a *App) loadLifeCursor(accID string) lifeCursor {
	if raw := a.DB.GetMeta(a.cursorKey(accID), ""); raw != "" {
		var c lifeCursor
		if err := json.Unmarshal([]byte(raw), &c); err == nil && c.Time > 0 {
			return c
		}
	}
	return lifeCursor{Time: time.Now().Unix() - 300}
}

func (a *App) saveLifeCursor(accID string, c lifeCursor) {
	if b, err := json.Marshal(c); err == nil {
		_ = a.DB.SetMeta(a.cursorKey(accID), string(b))
	}
}

// ---------------------------------------------------------------- 路径匹配

// pathMatch 是一条映射命中的结果：localPrefix 是本地目录（相对映射根），
// relDir 是事件所在目录相对「网盘监控目录」的部分。
type pathMatch struct {
	localPrefix string
	relDir      string
}

// matchRemotePath 判断网盘绝对路径是否落在配置的某个监控目录下。
func (a *App) matchRemotePath(c *model.StrmConfig, remotePath string) (pathMatch, bool) {
	rp := cleanRemote(remotePath)
	for _, p := range c.EffectivePaths() {
		base := cleanRemote(p.RemotePath)
		prefix := a.Store.CleanRel(p.LocalDir)
		if base == "" {
			return pathMatch{localPrefix: prefix, relDir: strings.TrimPrefix(rp, "/")}, true
		}
		if rp == base || strings.HasPrefix(rp, base+"/") {
			rel := strings.TrimPrefix(rp, base)
			return pathMatch{localPrefix: prefix, relDir: strings.TrimPrefix(rel, "/")}, true
		}
	}
	return pathMatch{}, false
}

func cleanRemote(p string) string {
	p = strings.ReplaceAll(strings.TrimSpace(p), "\\", "/")
	if p == "" || p == "/" {
		return ""
	}
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	for strings.Contains(p, "//") {
		p = strings.ReplaceAll(p, "//", "/")
	}
	return strings.TrimSuffix(p, "/")
}

// ---------------------------------------------------------------- 一轮监控

// lifeWatch 跑一轮：拉事件 → 分发 → 推进游标。
// 返回 false 表示事件接口不可用，调用方应退回目录指纹探测。
func (a *App) lifeWatch(ctx context.Context, accID string, cfgs []*model.StrmConfig) bool {
	if len(cfgs) == 0 || ctx.Err() != nil {
		return true
	}
	client, err := a.client(accID)
	if err != nil {
		a.Event("warn", "strm", "事件监控：取不到账号客户端（"+err.Error()+"）", "")
		return true // 账号问题退化成扫描也一样失败，别刷屏
	}
	cur := a.loadLifeCursor(accID)
	events, err := client.LifeEvents(ctx, cur.Time, cur.ID, 400)
	if err != nil {
		a.Event("warn", "strm", fmt.Sprintf("事件监控：拉取 115 事件失败，本轮改用目录扫描（%v）", err), "")
		return false
	}
	// 先把各条配置的监控目录登记成「锚点」：事件只给 parent_id，
	// 从这个锚点往上没有名字也没关系，路径前缀跟配置里写的保持一致就能匹配。
	for _, c := range cfgs {
		for _, p := range c.EffectivePaths() {
			client.SetDirPath(p.RemoteID, p.RemotePath)
		}
	}
	touch := func() {
		for _, c := range cfgs {
			_ = a.DB.UpdateConfig(c.ID, map[string]any{"last_check_at": time.Now().Unix()})
		}
	}
	if len(events) == 0 {
		// 没有新事件：游标推到「一分钟前」，既省掉重复查询，又留一点服务端延迟的余量
		if cur.Time < time.Now().Unix()-60 {
			a.saveLifeCursor(accID, lifeCursor{Time: time.Now().Unix() - 60})
		}
		touch()
		return true
	}

	// 目录路径按 cid 缓存一轮；元数据目录列表也缓存（同目录可能有连续几个事件）
	dirCache := map[string]string{}
	dirOf := func(cid string) (string, bool) {
		if v, ok := dirCache[cid]; ok {
			return v, true
		}
		p, err := client.DirPath(ctx, cid)
		if err != nil {
			return "", false
		}
		dirCache[cid] = p
		return p, true
	}
	metaCache := map[string][]p115.Node{}
	needMeta := func() bool {
		for _, c := range cfgs {
			if c.DownloadMeta || a.Settings().DownloadMeta {
				return true
			}
		}
		return false
	}
	metaOf := func(cid string) []p115.Node {
		if v, ok := metaCache[cid]; ok {
			return v
		}
		var out []p115.Node
		if needMeta() {
			exts := map[string]bool{}
			for _, c := range cfgs {
				for k := range a.metaExts(c) {
					exts[k] = true
				}
			}
			if len(exts) == 0 {
				exts = nil
			}
			if nodes, err := client.ListDir(ctx, cid); err == nil {
				for _, n := range nodes {
					if !n.IsDir && matchMeta(n.Name, exts) {
						out = append(out, n)
					}
				}
			}
		}
		metaCache[cid] = out
		return out
	}

	var created, removed, renamed, rescanned, unresolved int
	for _, e := range events {
		dirPath, ok := dirOf(e.ParentID)
		if !ok {
			// 删除事件不需要父路径：115 的删除事件 parent_id 经常是 0 或已被删掉的
			// 目录，怎么拼都拼不出来，但 lifeDelete 是按 file_id 查索引删的，
			// 完全能处理。以前把这种也算「拼不出来」，一条删除事件就让整轮降级成
			// 目录扫描（大树要列好几分钟），纯属自己吓自己。
			if e.Type == p115.LifeDelete {
				dirPath = ""
			} else {
				unresolved++
				log.Printf("事件监控：目录 %q 的路径拼不出来（事件类型 %d，文件 %q），跳过", e.ParentID, e.Type, e.Name)
				continue
			}
		}
		switch {
		case e.Type == p115.LifeDelete:
			removed += a.lifeDelete(cfgs, e, dirPath)
		case e.Type == p115.LifeRenameFile || e.Type == p115.LifeMoveFile || e.Type == p115.LifeMoveImage:
			renamed += a.lifeRelocate(client, cfgs, e, dirPath)
			created += a.lifeCreate(client, cfgs, e, dirPath, metaOf)
		case e.Type == p115.LifeRenameDir:
			renamed += a.lifeRelocateDir(client, cfgs, e, dirPath)
		case e.IsFolderEvent():
			// 新目录 / 复制目录：子文件不一定有各自的事件，补一次增量扫描
			for _, c := range cfgs {
				if _, ok := a.matchRemotePath(c, dirPath); !ok {
					continue
				}
				if a.triggerInc(c) {
					rescanned++
				}
			}
		case e.IsCreate():
			created += a.lifeCreate(client, cfgs, e, dirPath, metaOf)
		}
	}

	// 游标推进到本轮最新的一条；如果一次没拉完（撞上限），退到最老的一条，
	// 下一轮接着往下追——重复处理是幂等的，丢事件才是真丢。
	cursor := lifeCursor{Time: events[len(events)-1].Time, ID: events[len(events)-1].ID}
	if len(events) >= 400 {
		cursor = lifeCursor{Time: events[0].Time, ID: events[0].ID - 1}
	}
	if cursor.Time > 0 || cursor.ID > 0 {
		a.saveLifeCursor(accID, cursor)
	}
	touch()

	if unresolved > 0 {
		a.Event("warn", "strm", fmt.Sprintf("事件监控：%d 条事件的目录路径拼不出来，本轮改用目录扫描补全", unresolved), "")
	}
	if created+removed+renamed+rescanned > 0 {
		msg := fmt.Sprintf("事件监控：新增 %d · 删除 %d · 改名/移动 %d", created, removed, renamed)
		if rescanned > 0 {
			msg += fmt.Sprintf(" · 新目录补扫 %d", rescanned)
		}
		a.Event("info", "strm", msg, "")
	}
	// 有事件没能处理，就让调用方跑一轮目录扫描兜底（扫描顺带把目录树缓存补全）
	return unresolved == 0
}

// ---------------------------------------------------------------- 单个事件

// lifeCreate 处理「多了一个文件」：写 strm + 落索引。
func (a *App) lifeCreate(client *p115.Client, cfgs []*model.StrmConfig, e p115.LifeEvent, dirPath string, metaOf func(string) []p115.Node) int {
	if e.IsDir || e.FileID == "" || e.Name == "" {
		return 0
	}
	uid := client.UID(context.Background())
	n := p115.Node{
		ID: e.FileID, ParentID: e.ParentID, Name: e.Name, Pickcode: e.Pickcode,
		Sha1: e.Sha1, Size: e.Size,
	}
	done := 0
	for _, c := range cfgs {
		m, ok := a.matchRemotePath(c, dirPath)
		if !ok || !strm.MatchExt(e.Name, a.strmExts(c), c.AllFiles) {
			continue
		}
		if n.Pickcode == "" {
			if d, err := client.Details(context.Background(), e.FileID); err == nil {
				n.Pickcode = d.Pickcode
				n.Sha1 = firstNonEmptyStr(n.Sha1, d.Sha1)
				n.Size = d.Size
			}
		}
		if n.Pickcode == "" {
			continue
		}
		rel := path.Join(m.relDir, strm.Sanitize(e.Name, 200))
		if _, _, err := a.writeStrmFor(c, m.localPrefix, rel, n, uid, metaOf(e.ParentID)); err != nil {
			a.Event("warn", "strm", fmt.Sprintf("事件监控：写 %s 失败（%v）", e.Name, err), "")
			continue
		}
		done++
	}
	return done
}

// lifeDelete 处理删除：先按文件 id 在索引里找（115 的删除事件只给 file_id，
// parent_id 常常是 0，靠路径匹配不可靠），找到就删 strm 和索引；
// 索引里没有的目录删除，再按路径前缀整片收掉。
func (a *App) lifeDelete(cfgs []*model.StrmConfig, e p115.LifeEvent, dirPath string) int {
	removed := 0
	hit := false
	for _, c := range cfgs {
		row, _ := a.DB.FindFileByFileID(c.ID, e.FileID)
		if row == nil {
			continue
		}
		a.dropFile(c, row)
		removed++
		hit = true
	}
	if hit {
		return removed
	}
	// 删的是目录：115 的删除事件 parent_id 常是 0，拼不出父路径，
	// 改用 parent_id 命中——目录下直接子文件的 parent_id 就是这个目录。
	if e.IsDir && e.FileID != "" {
		for _, c := range cfgs {
			removed += a.dropSubtree(c, e.FileID)
		}
		if removed > 0 {
			return removed
		}
	}
	// 最后才按路径前缀兜底。dirPath 为空说明父目录已经拼不出来（目录被删、
	// 或者压根不在监控范围），此时 matchRemotePath("") 会命中「监控根目录」，
	// 拿一个空 relDir 去比名字很容易误删同名文件 —— 宁可不删。
	if dirPath == "" {
		return removed
	}
	for _, c := range cfgs {
		m, ok := a.matchRemotePath(c, dirPath)
		if !ok {
			continue
		}
		prefix := path.Join(m.relDir, strm.Sanitize(e.Name, 200))
		for _, row := range a.DB.ListFiles(c.ID) {
			if row.RelPath == prefix || strings.HasPrefix(row.RelPath, prefix+"/") {
				a.dropFile(c, &row)
				removed++
			}
		}
	}
	return removed
}

// dropSubtree 按「目录 cid」删掉索引里这个目录下的整棵子树。
func (a *App) dropSubtree(c *model.StrmConfig, dirID string) int {
	rows := a.DB.ListFiles(c.ID)
	oldDir := ""
	for _, row := range rows {
		if row.ParentID == dirID {
			oldDir = path.Dir(row.RelPath)
			break
		}
	}
	if oldDir == "" || oldDir == "." {
		return 0
	}
	n := 0
	for _, row := range rows {
		if row.RelPath == oldDir || strings.HasPrefix(row.RelPath, oldDir+"/") {
			a.dropFile(c, &row)
			n++
		}
	}
	return n
}

// lifeRelocate 处理文件改名 / 移动：旧位置的 strm 收掉，新位置由 lifeCreate 补。
func (a *App) lifeRelocate(client *p115.Client, cfgs []*model.StrmConfig, e p115.LifeEvent, dirPath string) int {
	if e.IsDir {
		return a.lifeRelocateDir(client, cfgs, e, dirPath)
	}
	done := 0
	for _, c := range cfgs {
		row, _ := a.DB.FindFileByFileID(c.ID, e.FileID)
		if row == nil {
			continue
		}
		// 新位置还在监控范围里吗？在就换个路径写回去（内容不变），不在就删掉
		m, inScope := a.matchRemotePath(c, dirPath)
		if inScope {
			rel := path.Join(m.relDir, strm.Sanitize(e.Name, 200))
			if rel == row.RelPath {
				continue
			}
		}
		a.dropFile(c, row)
		done++
	}
	return done
}

// lifeRelocateDir 处理目录改名 / 搬家。
//
// 索引里没有目录行，但目录下直接子文件的 parent_id 就是这个目录的 cid，
// 拿其中一行就能推出旧前缀，然后把整棵子树的行与 strm 一起挪到新前缀。
func (a *App) lifeRelocateDir(client *p115.Client, cfgs []*model.StrmConfig, e p115.LifeEvent, dirPath string) int {
	client.ForgetPaths()
	uid := client.UID(context.Background())
	done := 0
	for _, c := range cfgs {
		m, ok := a.matchRemotePath(c, dirPath)
		if !ok {
			continue
		}
		newDir := path.Join(m.relDir, strm.Sanitize(e.Name, 200))
		rows := a.DB.ListFiles(c.ID)
		oldDir := ""
		for _, row := range rows {
			if row.ParentID == e.FileID {
				oldDir = path.Dir(row.RelPath)
				break
			}
		}
		if oldDir == "" || oldDir == "." || oldDir == newDir {
			continue
		}
		for _, row := range rows {
			if row.RelPath != oldDir && !strings.HasPrefix(row.RelPath, oldDir+"/") {
				continue
			}
			tail := strings.TrimPrefix(row.RelPath, oldDir)
			newRel := path.Join(newDir, tail)
			abs, _, err := a.Store.Write(a.strmAbsPath(c, m.localPrefix, newRel), a.PlayURLWith(c.StrmBase, row.Pickcode, row.Name, uid))
			if err != nil {
				continue
			}
			if row.StrmPath != "" {
				_, _ = a.Store.RemoveAbs(row.StrmPath, a.Settings().DeleteMetaWithStrm)
			}
			newRow := row
			newRow.RelPath = newRel
			newRow.StrmPath = abs
			_ = a.DB.UpsertFile(newRow)
			done++
		}
	}
	return done
}

// strmAbsPath 拼出 strm 的相对路径（带 .strm）。
func (a *App) strmAbsPath(c *model.StrmConfig, prefix, rel string) string {
	p := path.Join(prefix, rel)
	if !strings.HasSuffix(strings.ToLower(p), ".strm") {
		p += ".strm"
	}
	return p
}

// dropFile 删掉一条索引对应的 strm 与索引行。
func (a *App) dropFile(c *model.StrmConfig, row *model.StrmFile) {
	if row == nil {
		return
	}
	if row.StrmPath != "" {
		_, _ = a.Store.RemoveAbs(row.StrmPath, a.Settings().DeleteMetaWithStrm)
	}
	_ = a.DB.DeleteFileRow(row.ID)
}

// triggerInc 异步跑一次增量，正在跑就跳过。
func (a *App) triggerInc(c *model.StrmConfig) bool {
	if a.IsRunning(c.ID) {
		return false
	}
	log.Printf("事件监控：%s 目录有新增，补一次增量", c.Name)
	ok, _ := a.RunStrm(c.ID, ModeInc)
	return ok
}
