package service

import (
	"context"
	"fmt"
	"hash/fnv"
	"time"
)

// 同播复制：同一个文件有多台设备同时播放时，给后来的设备在 115 里复制一份，
// 各走各的直链、不抢带宽。对这个账号的所有播放生效（strm / ed2k 都算）。

const (
	sessionTTL = 3 * time.Minute
	copyTTL    = 40 * time.Minute
)

// DeviceKey 用「客户端 IP + UA」标识一台设备。
func DeviceKey(ip, ua string) string {
	h := fnv.New32a()
	_, _ = h.Write([]byte(ip))
	_, _ = h.Write([]byte("|"))
	_, _ = h.Write([]byte(ua))
	return fmt.Sprintf("%x", h.Sum32())
}

// ResolvePickcode 处理同播复制并返回本次播放应该用的 pickcode。
// copyPlay 为 false 时原样返回。
func (a *App) ResolvePickcode(ctx context.Context, accountID string, copyPlay bool, pickcode, device string) string {
	if !copyPlay || pickcode == "" {
		return pickcode
	}
	now := time.Now()

	a.copyMu.Lock()
	sess := a.sessions[pickcode]
	if sess == nil {
		sess = map[string]time.Time{}
		a.sessions[pickcode] = sess
	}
	for d, t := range sess {
		if now.Sub(t) > sessionTTL {
			delete(sess, d)
		}
	}
	others := 0
	for d := range sess {
		if d != device {
			others++
		}
	}
	cacheKey := device + "|" + pickcode
	if ent, ok := a.copyCache[cacheKey]; ok && now.Before(ent.expire) {
		sess[device] = now
		a.copyMu.Unlock()
		return ent.pickcode
	}
	sess[device] = now
	a.copyMu.Unlock()

	if others == 0 {
		return pickcode
	}

	newCode, err := a.copyFileFor(ctx, accountID, pickcode)
	if err != nil {
		a.Event("warn", "copy", fmt.Sprintf("同播复制失败（继续用原文件）：%v", err), "")
		return pickcode
	}
	a.copyMu.Lock()
	a.copyCache[cacheKey] = copyEntry{pickcode: newCode, expire: now.Add(copyTTL)}
	a.copyMu.Unlock()
	a.Event("info", "copy", fmt.Sprintf("同播复制：%s → 新副本 %s", pickcode, newCode), "")
	return newCode
}

// copyFileFor 在 115 里把文件复制一份（同目录），返回新文件的 pickcode。
func (a *App) copyFileFor(ctx context.Context, accountID, pickcode string) (string, error) {
	client, err := a.client(accountID)
	if err != nil {
		return "", err
	}
	row, _ := a.DB.FindFileByPickcode(pickcode)
	fileID, parentID := "", ""
	if row != nil {
		fileID, parentID = row.FileID, row.ParentID
	}
	if fileID == "" {
		n, err := client.Details(ctx, pickcode)
		if err != nil {
			return "", err
		}
		fileID, parentID = n.ID, n.ParentID
	}
	if parentID == "" {
		parentID = "0"
	}
	before, _ := client.ListDir(ctx, parentID)
	beforeIDs := map[string]bool{}
	for _, n := range before {
		beforeIDs[n.ID] = true
	}
	if err := client.Copy(ctx, []string{fileID}, parentID); err != nil {
		return "", err
	}
	// 115 建副本是异步的，重试几次直到目录里出现新文件
	for attempt := 0; attempt < 8; attempt++ {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(time.Second):
		}
		nodes, err := client.ListDir(ctx, parentID)
		if err != nil {
			continue
		}
		for _, n := range nodes {
			if n.IsDir || beforeIDs[n.ID] {
				continue
			}
			code := n.Pickcode
			if code == "" {
				if d, err := client.Details(ctx, n.ID); err == nil {
					code = d.Pickcode
				}
			}
			if code != "" {
				return code, nil
			}
		}
	}
	return "", fmt.Errorf("复制成功但没在目录里找到新文件")
}

// CopyNow 页面上的「立即复制一份」按钮。
func (a *App) CopyNow(ctx context.Context, pickcode string) (string, error) {
	row, _ := a.DB.FindFileByPickcode(pickcode)
	accountID := ""
	if row != nil {
		accountID = row.AccountID
	}
	return a.copyFileFor(ctx, accountID, pickcode)
}
