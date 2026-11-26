package service

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
)

// 115 秒传：把源账号里的文件，用「sha1 + size」在目标账号里瞬间生成一份。
//
// 播放链路：Emby 用户 → 映射账号（instant_map）→ 目标目录（instant_targets）
// → 先秒传到目标目录 → 再按目标目录里的文件播放。

var instantLocks sync.Map // "sha1|account" -> *sync.Mutex

func (a *App) InstantTargets() []model.InstantTarget        { return a.DB.ListInstantTargets() }
func (a *App) InstantMappings() []model.InstantMapping      { return a.DB.ListInstantMap() }
func (a *App) DeleteInstantTarget(id string) error          { return a.DB.DeleteInstantTarget(id) }
func (a *App) DeleteInstantMapping(id string) error         { return a.DB.DeleteInstantMap(id) }

func (a *App) SaveInstantTarget(t model.InstantTarget) error {
	if t.AccountID == "" {
		return fmt.Errorf("请选择被秒传账号")
	}
	if t.DirCID == "" {
		t.DirCID = "0"
	}
	return a.DB.SaveInstantTarget(t)
}

func (a *App) SaveInstantMapping(m model.InstantMapping) error {
	if m.EmbyUserID == "" || m.AccountID == "" {
		return fmt.Errorf("请选择 Emby 用户与账号")
	}
	return a.DB.SaveInstantMap(m)
}

// InstantTargetFor 找账号的秒传落盘目录。
func (a *App) InstantTargetFor(accountID string) *model.InstantTarget {
	for _, t := range a.DB.ListInstantTargets() {
		if t.AccountID == accountID && t.Enabled {
			return &t
		}
	}
	return nil
}

// EnsureInstant 确保 file 已在映射账号的目标目录里，返回可播放的 pickcode。
// 返回空 pickcode 且 err=nil 表示「这个用户没有映射，走普通播放」。
func (a *App) EnsureInstant(ctx context.Context, userID string, file *model.StrmFile) (string, string, error) {
	if file == nil {
		return "", "", nil
	}
	accountID := a.DB.InstantAccountForUser(userID)
	if accountID == "" {
		return "", "", nil
	}
	pick, err := a.instantCopy(ctx, accountID, file)
	return pick, accountID, err
}

// InstantDeduped 按 sha1 秒传：文件已经在目标账号就直接返回它的 pickcode。
//
// 给 /p115/instant/<sha1> 路由用 —— 那条路上的地址里只有 sha1，
// 源账号、文件名、大小都从索引里补回来。
func (a *App) InstantDeduped(ctx context.Context, accountID, sha1, name string, size int64, sourceAccount, sourcePick string) (string, error) {
	sha1 = strings.ToUpper(strings.TrimSpace(sha1))
	if sha1 == "" {
		return "", fmt.Errorf("缺少 sha1，无法秒传")
	}
	if accountID == "" {
		return "", fmt.Errorf("未指定秒传账号")
	}
	if row, _ := a.DB.FindFileBySha1(sha1, accountID); row != nil && row.Pickcode != "" {
		return row.Pickcode, nil
	}
	file := &model.StrmFile{
		Sha1: sha1, Name: name, Size: size,
		AccountID: sourceAccount, Pickcode: sourcePick,
	}
	// 索引里补回缺的字段：源 pickcode / 文件名 / 大小
	if row, _ := a.DB.FindFileBySha1(sha1, sourceAccount); row != nil {
		if file.Name == "" {
			file.Name = row.Name
		}
		if file.Size <= 0 {
			file.Size = row.Size
		}
		if file.Pickcode == "" {
			file.Pickcode = row.Pickcode
		}
	}
	if file.Name == "" {
		file.Name = sha1 + ".mp4"
	}
	if file.Size <= 0 {
		return "", fmt.Errorf("索引里没有 %s 的大小，无法秒传", sha1[:12])
	}
	return a.instantCopy(ctx, accountID, file)
}

// instantCopy 是秒传的底层实现：把 file 复制进 accountID 的目标目录。
func (a *App) instantCopy(ctx context.Context, accountID string, file *model.StrmFile) (string, error) {
	target := a.InstantTargetFor(accountID)
	if target == nil {
		return "", fmt.Errorf("账号没有配置秒传目录")
	}
	sha1 := strings.ToUpper(file.Sha1)
	if sha1 == "" && file.Pickcode != "" {
		if c, err := a.client(file.AccountID); err == nil {
			if n, err := c.Details(ctx, file.Pickcode); err == nil {
				sha1 = strings.ToUpper(n.Sha1)
			}
		}
	}
	if sha1 == "" {
		return "", fmt.Errorf("拿不到文件的 sha1，无法秒传")
	}
	file.Sha1 = sha1

	// 已经秒传过：直接用缓存
	if row, _ := a.DB.FindFileBySha1(sha1, accountID); row != nil && row.Pickcode != "" {
		return row.Pickcode, nil
	}

	// 同一个文件的秒传只允许一个线程在做：多个用户同时点同一集时，
	// 后面的等前面的做完，直接拿结果，不要重复传校验块。
	lockKey := sha1 + "|" + accountID
	v, _ := instantLocks.LoadOrStore(lockKey, &sync.Mutex{})
	mu := v.(*sync.Mutex)
	mu.Lock()
	defer mu.Unlock()
	if row, _ := a.DB.FindFileBySha1(sha1, accountID); row != nil && row.Pickcode != "" {
		return row.Pickcode, nil
	}

	targetClient, err := a.client(accountID)
	if err != nil {
		return "", err
	}
	sourceClient, err := a.client(file.AccountID)
	if err != nil {
		return "", err
	}
	a.Event("info", "instant", fmt.Sprintf("开始秒传：%s → 账号 %s", file.Name, accountID), "")

	fetcher := func(signCheck string) ([]byte, error) {
		return a.readRange(ctx, sourceClient, file, signCheck)
	}
	size := file.Size
	pickcode, err := targetClient.RapidUpload(ctx, target.DirCID, file.Name, sha1, size, fetcher)
	if err != nil {
		// 秒传是按 sha1 校验的，被拒的常见原因是**源盘把文件换了**，
		// 索引里记的 sha1 已经过时。重新问一次源盘拿当前值，不一样就当场
		// 再秒传一次 —— 这样源盘换了文件时这一集照样能看，而不是白等一轮。
		if n, derr := sourceClient.Details(ctx, file.Pickcode); derr == nil && n != nil {
			fresh := strings.ToUpper(n.Sha1)
			if fresh != "" && fresh != sha1 {
				log.Printf("秒传：115 不认 %s 的旧校验值，按新 sha1=%s 重试一次", file.Name, fresh[:12])
				sha1 = fresh
				file.Sha1 = fresh
				if n.Size > 0 {
					size = n.Size
					file.Size = n.Size
				}
				pickcode, err = targetClient.RapidUpload(ctx, target.DirCID, file.Name, sha1, size, fetcher)
			}
		}
	}
	if err != nil {
		return "", err
	}
	_ = a.DB.UpsertFile(model.StrmFile{
		ConfigID: "instant", AccountID: accountID, FileID: sha1, Pickcode: pickcode,
		Name: file.Name, RelPath: file.RelPath, Size: file.Size, Sha1: sha1,
		Source: "instant",
	})
	a.Event("success", "instant", fmt.Sprintf("秒传完成：%s（账号 %s）", file.Name, accountID), "")
	return pickcode, nil
}

// instantPlan 是「这个播放地址该不该秒传、秒传到哪」的结论。
type instantPlan struct {
	account string
	file    *model.StrmFile
}

// instantPlanFor 判断一个源账号播放地址要不要改写成秒传路由。
func (a *App) instantPlanFor(raw, embyUser string) *instantPlan {
	if raw == "" || embyUser == "" {
		return nil
	}
	accountID := a.DB.InstantAccountForUser(embyUser)
	if accountID == "" {
		return nil
	}
	pick, ok := pickcodeOfPlayURL(raw)
	if !ok {
		return nil
	}
	file, err := a.DB.FindFileByPickcode(pick)
	if err != nil || file == nil {
		return nil
	}
	if strings.ToUpper(file.Sha1) == "" || file.Size <= 0 {
		return nil
	}
	// 文件本来就在目标账号里，直接用它自己的地址播就行
	if file.AccountID == accountID {
		return nil
	}
	return &instantPlan{account: accountID, file: file}
}

// InstantPathFor 把源账号的播放地址改写成秒传路由。
//
// 输入形如 https://host/p115/play/<pickcode>/<名字>/<115 uid>，
// 输出 https://host/p115/instant/<sha1>.<ext>?account=...&size=...&name=...
//
// **只换路径，scheme 与 host 原样保留**：这个 host 是 strm 里写的地址，
// 客户端既然能访问它，换完也照样能访问（不像另拼 base url 那样，配错了
// 就变成播放器连不上的内网地址）。
//
// 返回空串表示不该走秒传（没有映射、文件不在索引里、没有 sha1、或本来就在
// 目标账号里），调用方按原地址播即可。
func (a *App) InstantPathFor(raw, embyUser string) string {
	plan := a.instantPlanFor(raw, embyUser)
	if plan == nil {
		return ""
	}
	return buildInstantPath(raw, a.BaseURL(), strings.ToUpper(plan.file.Sha1),
		plan.file.Name, plan.file.Size, plan.account, plan.file.AccountID)
}

// PrewarmInstant 在后台先把文件秒传好。
//
// PlaybackInfo 到真正拉流之间通常隔一两秒（播放器要准备解码器），
// 这段时间正好用来秒传。等播放器请求 /p115/instant 时文件已经在目标
// 账号里了，首帧几乎不用等；秒传失败也无所谓 —— 真到播放时会重试一次。
func (a *App) PrewarmInstant(raw, embyUser string) {
	// 「115 秒传」页面上的预秒传开关。关掉后 PlaybackInfo 不再提前动手，
	// 等播放器真正来拉 /p115/instant 时再同步秒传（首播多等两三秒，
	// 但不会因为一次误点播放就白占目标账号的目录）。
	if a.Settings().InstantPrewarmOff {
		return
	}
	plan := a.instantPlanFor(raw, embyUser)
	if plan == nil {
		return
	}
	sha1 := strings.ToUpper(plan.file.Sha1)
	if row, _ := a.DB.FindFileBySha1(sha1, plan.account); row != nil && row.Pickcode != "" {
		return // 已经秒传过，不用预热
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if _, err := a.instantCopy(ctx, plan.account, plan.file); err != nil {
			log.Printf("秒传预热失败（播放时会再试）：%v", err)
		}
	}()
}

// buildInstantPath 拼秒传路由：<原 scheme/host>/p115/instant/<sha1>.<ext>?account=..
//
// 只换路径、保留原 scheme/host 是刻意的：这个 host 来自 strm（客户端本来
// 就能访问），换完照旧；另拼配置里的 base url 反而常常是 docker 内网地址。
// 原地址是相对的（没有 scheme/host）时，用 base 补全。
func buildInstantPath(raw, base, sha1, name string, size int64, accountID, sourceAccount string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	if u.Scheme == "" && u.Host == "" {
		bu, berr := url.Parse(strings.TrimRight(base, "/"))
		if berr != nil || bu.Host == "" {
			return ""
		}
		u.Scheme = bu.Scheme
		u.Host = bu.Host
	}
	ext := ""
	if e := path.Ext(name); e != "" && len(e) <= 11 {
		ext = e
	}
	q := url.Values{}
	q.Set("account", accountID)
	q.Set("size", strconv.FormatInt(size, 10))
	q.Set("name", name)
	if sourceAccount != "" {
		q.Set("source", sourceAccount)
	}
	u.Path = "/p115/instant/" + sha1 + ext
	u.RawQuery = q.Encode()
	u.Fragment = ""
	return u.String()
}

// ApplyInstantForEmby 是播放链路里的秒传入口。
//
// Emby 反代拿到候选播放地址（形如 /p115/play/{pickcode}/{name}/{uid}）后先过这里：
// 如果这个 Emby 用户配了「映射账号 + 秒传目录」，就把文件秒传到那个账号，
// 再把地址改写成目标账号的 pickcode / uid，后续仍由原来的 /play 解析直链。
//
// 返回改写后的地址与「是否改写」。任何一步不通都原样返回，不影响普通播放。
func (a *App) ApplyInstantForEmby(ctx context.Context, embyUser, candidate string) (string, bool) {
	if embyUser == "" || candidate == "" {
		return candidate, false
	}
	accountID := a.DB.InstantAccountForUser(embyUser)
	if accountID == "" {
		return candidate, false
	}
	pick, ok := pickcodeOfPlayURL(candidate)
	if !ok {
		// 分享直链（/s/...）或天翼 / alist 之类，不归秒传管
		return candidate, false
	}
	file, err := a.DB.FindFileByPickcode(pick)
	if err != nil || file == nil {
		log.Printf("秒传跳过：strm 里没有 pickcode=%s 的记录（%v）", pick, err)
		return candidate, false
	}
	// 文件本来就在目标账号里，不用秒传
	if file.AccountID == accountID {
		return candidate, false
	}
	acc, err := a.DB.GetAccount(accountID)
	if err != nil || acc == nil {
		return candidate, false
	}
	// 账号表里的 uid 可能还没回填（老账号/没刷新过），这时从 cookie 里取
	targetUID := p115.CleanUID(acc.UID)
	if targetUID == "" {
		if c, cerr := a.client(accountID); cerr == nil {
			targetUID = p115.CleanUID(c.UID(ctx))
		}
	}
	if targetUID == "" {
		log.Printf("秒传跳过：账号 %s 拿不到 115 uid", accountID)
		return candidate, false
	}

	newPick, _, err := a.EnsureInstant(ctx, embyUser, file)
	if err != nil {
		a.Event("warn", "instant", fmt.Sprintf("秒传失败：%s（%v）", file.Name, err), "")
		return candidate, false
	}
	if newPick == "" || newPick == pick {
		return candidate, false
	}
	// EnsureInstant 内部已经记了「开始秒传 / 秒传完成」，这里不重复发事件，
	// 否则每次播放都会多一条。
	out := rewritePlayURL(candidate, newPick, targetUID)
	log.Printf("秒传播放：%s → 账号 %s（%s）", file.Name, accountID, newPick)
	return out, true
}

// pickcodeOfPlayURL 从 /p115/play/{pickcode}/{name}/{uid} 里取 pickcode。
func pickcodeOfPlayURL(raw string) (string, bool) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", false
	}
	seg := strings.Split(strings.Trim(u.Path, "/"), "/")
	for i := 0; i+1 < len(seg); i++ {
		if seg[i] == "play" {
			if p := seg[i+1]; p != "" {
				return p, true
			}
			return "", false
		}
	}
	return "", false
}

// rewritePlayURL 把播放地址换成新的 pickcode 与账号 uid，其余（scheme/host/文件名）不动。
func rewritePlayURL(raw, pickcode, uid string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	seg := strings.Split(strings.Trim(u.Path, "/"), "/")
	for i := 0; i+1 < len(seg); i++ {
		if seg[i] != "play" {
			continue
		}
		seg[i+1] = pickcode
		seg[len(seg)-1] = uid
		u.Path = "/" + strings.Join(seg, "/")
		return u.String()
	}
	return raw
}

// instantBlockSize 是校验缓存的块大小。115 要的区间通常落在文件开头几十万
// 字节处，按整块取回来存住，之后它再挑同一块里的任何区间都不用碰源盘。
const instantBlockSize = 1 << 20

func instantMin(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func instantMax(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

// readRange 从源文件直链读取 115 要求的校验区间（结果按块缓存）。
//
// 顺序上必须按块从低到高拼，不能「先拼命中的、再拼回源的」—— 中间块命中、
// 前后块缺失时那样拼出来的字节顺序是乱的，sha1 自然对不上。
func (a *App) readRange(ctx context.Context, source *p115.Client, file *model.StrmFile, signCheck string) ([]byte, error) {
	spec := strings.TrimSpace(signCheck)
	start, end, err := parseSignCheck(spec)
	if err != nil {
		return nil, err
	}
	sha1 := strings.ToUpper(file.Sha1)

	// 1. 先看缓存，然后按「块内需要的部分」回源。
	//
	// **不要整块读**：115 只要 10~200KB 的一小段，而它家 CDN 对这类
	// Range 请求限速得厉害（实测 1MB 要 10~20 秒）。整块读看着能进缓存，
	// 实际比只读需要的多花 6 倍时间，而 115 每次挑的区间还不一样，
	// 缓存几乎命中不了 —— 首次播放白等十几秒，全耗在这。
	//
	// 所以：缓存里有的块直接用；缺的部分只读真正需要的那几十 KB。
	// 只有「恰好整块」时才顺手存进缓存（下次同块还能命中）。
	pickcode := ""
	getLink := func() (string, map[string]string, error) {
		if pickcode == "" {
			pickcode = file.Pickcode
			if pickcode == "" {
				if n, derr := source.Details(ctx, file.FileID); derr == nil {
					pickcode = n.Pickcode
				}
			}
			if pickcode == "" {
				return "", nil, fmt.Errorf("源文件没有 pickcode")
			}
		}
		return source.DownloadURL(ctx, pickcode, "", 5*time.Minute)
	}

	want := end - start + 1
	out := make([]byte, 0, want)
	var hc *http.Client
	// 连续的缺失段攒起来一次读掉：115 的区间常常跨块，按块拆成两次请求
	// 就要付两次「首字节 + 限速」的钱，合并后只付一次。
	pendingStart, pendingEnd := int64(-1), int64(-1)
	flush := func() error {
		if pendingStart < 0 {
			return nil
		}
		link, header, derr := getLink()
		if derr != nil {
			return derr
		}
		if hc == nil {
			hc = &http.Client{Timeout: 90 * time.Second}
		}
		t0 := time.Now()
		chunk, ferr := fetchRange(ctx, hc, link, header, pendingStart, pendingEnd)
		if ferr != nil {
			return ferr
		}
		log.Printf("秒传：校验段 %d-%d 回源 %s（%d 字节，host=%s）",
			pendingStart, pendingEnd, time.Since(t0).Round(time.Millisecond), len(chunk), hostOf(link))
		// 正好覆盖一整块时才存缓存（下次 115 挑同一块就不用再读）
		if pendingEnd-pendingStart+1 == instantBlockSize && pendingStart%instantBlockSize == 0 &&
			int64(len(chunk)) == instantBlockSize {
			if err := a.DB.DigestBlockPut(sha1, pendingStart, chunk); err != nil {
				log.Printf("秒传：校验块写缓存失败（%v）", err)
			}
		}
		out = append(out, chunk...)
		pendingStart, pendingEnd = -1, -1
		return nil
	}

	for pos := start; pos <= end; {
		off := (pos / instantBlockSize) * instantBlockSize
		blockEnd := off + instantBlockSize - 1
		segEnd := instantMin(end, blockEnd)
		if data := a.DB.DigestBlockGet(sha1, off); len(data) > 0 {
			// 缓存命中：先把攒着的缺失段读掉，再拼缓存这一段
			if err := flush(); err != nil {
				return nil, err
			}
			lo := pos - off
			hi := segEnd - off
			if lo < int64(len(data)) {
				if hi >= int64(len(data)) {
					hi = int64(len(data)) - 1
				}
				out = append(out, data[lo:hi+1]...)
			}
		} else {
			if pendingStart < 0 {
				pendingStart = pos
			}
			pendingEnd = segEnd
		}
		pos = segEnd + 1
	}
	if err := flush(); err != nil {
		return nil, err
	}
	if int64(len(out)) > want {
		out = out[:want]
	}
	if int64(len(out)) < want {
		return nil, fmt.Errorf("校验数据不完整（要 %d 字节，只取到 %d）", want, len(out))
	}
	return out, nil
}

// parseSignCheck 解析 115 给的区间，形如 "148763209-148933163"；
// 也可能是 "0-"（读到文件尾），这时按一个块算。
func parseSignCheck(spec string) (int64, int64, error) {
	parts := strings.SplitN(spec, "-", 2)
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("校验区间格式不对: %s", spec)
	}
	start, err := strconv.ParseInt(strings.TrimSpace(parts[0]), 10, 64)
	if err != nil || start < 0 {
		return 0, 0, fmt.Errorf("校验区间格式不对: %s", spec)
	}
	endStr := strings.TrimSpace(parts[1])
	if endStr == "" {
		return start, start + instantBlockSize - 1, nil
	}
	end, err := strconv.ParseInt(endStr, 10, 64)
	if err != nil || end < start {
		return 0, 0, fmt.Errorf("校验区间格式不对: %s", spec)
	}
	return start, end, nil
}

// hostOf 取 URL 的 host，日志里用来判断是 CDN 还是中转。
func hostOf(raw string) string {
	if u, err := url.Parse(raw); err == nil {
		return u.Host
	}
	return "?"
}

// fetchRange 从源直链读 [start, end] 这一段。UA 必须和取链时一致，否则 115 CDN 403。
func fetchRange(ctx context.Context, hc *http.Client, link string, header map[string]string, start, end int64) ([]byte, error) {
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
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
		return nil, fmt.Errorf("读取校验数据失败（HTTP %d）", resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, int64(instantBlockSize)))
}
