package service

import (
	"context"
	"fmt"
	"os"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
)

// 联动删除：本地 strm 与 115 网盘文件双向对齐。
//
//   - 本地 strm 消失（超过宽限期）→ 删掉 115 上对应的文件
//   - 115 上删除后 → 清掉本地 strm（含同名 nfo / 海报 / 字幕）
//
// 每个方向都有独立开关，宽限期用来防止「扫描时磁盘刚好没挂上」误删云端文件。

// LinkScan 跑一轮对账。
func (a *App) LinkScan(ctx context.Context) {
	s := a.Settings()
	if !s.DeleteEnabled {
		return
	}
	a.Event("info", "link", "开始联动删除对账", "")
	totalLocal, totalCloud := 0, 0

	for _, cfg := range a.DB.ListConfigs() {
		if a.IsRunning(cfg.ID) {
			continue
		}
		client, err := a.client(cfg.AccountID)
		if err != nil {
			continue
		}
		rows := a.DB.ListFiles(cfg.ID)
		if len(rows) == 0 {
			continue
		}
		// 1) 云端现状
		remote := map[string]bool{}
		cid := cfg.RemoteID
		if cid == "" {
			cid = "0"
		}
		_ = client.Walk(ctx, cid, 0, func(n p115.Node, rel string) error {
			if !n.IsDir {
				remote[n.ID] = true
			}
			return nil
		})

		for _, row := range rows {
			if !remote[row.FileID] {
				// 云端没了 → 本地清掉
				if s.DeleteLocalWhenCloudDeleted && row.StrmPath != "" {
					if _, err := a.Store.RemoveAbs(row.StrmPath, s.DeleteMetaWithStrm); err == nil {
						totalCloud++
						a.DB.AddEvent(model.Event{Level: "info", Kind: "link",
							Message: fmt.Sprintf("云端已删除，清理本地 strm：%s", row.RelPath)})
					}
				}
				_ = a.DB.DeleteFileRow(row.ID)
				continue
			}
			// 2) 本地 strm 不见了 → 宽限期后删云端
			if _, err := os.Stat(row.StrmPath); err != nil && s.DeleteCloudWhenLocalMissing {
				if a.hasPendingDeletion(row.FileID) {
					continue
				}
				if s.DeleteGraceMinutes <= 0 {
					if err := client.Delete(ctx, []string{row.FileID}); err == nil {
						totalLocal++
						a.DB.AddEvent(model.Event{Level: "info", Kind: "link",
							Message: fmt.Sprintf("本地 strm 已删除，清理云端文件：%s", row.Name)})
						_ = a.DB.DeleteFileRow(row.ID)
					} else {
						a.DB.AddDeletion(model.Deletion{Kind: "local→cloud", AccountID: cfg.AccountID,
							FileID: row.FileID, Path: row.RelPath, StrmPath: row.StrmPath,
							State: "error", Message: err.Error()})
					}
				} else {
					a.DB.AddDeletion(model.Deletion{Kind: "local→cloud", AccountID: cfg.AccountID,
						FileID: row.FileID, Path: row.RelPath, StrmPath: row.StrmPath,
						State: "pending"})
				}
			}
		}
	}

	// 3) 宽限期到了的待办
	for _, d := range a.DB.ListDeletions() {
		if d.State != "pending" {
			continue
		}
		if time.Since(time.Unix(d.CreatedAt, 0)) < time.Duration(s.DeleteGraceMinutes)*time.Minute {
			continue
		}
		client, err := a.client(d.AccountID)
		if err != nil {
			continue
		}
		if err := client.Delete(ctx, []string{d.FileID}); err != nil {
			_ = a.DB.UpdateDeletion(d.ID, map[string]any{"state": "error", "message": err.Error()})
			continue
		}
		_ = a.DB.UpdateDeletion(d.ID, map[string]any{"state": "done"})
		totalLocal++
		a.Event("info", "link", fmt.Sprintf("宽限期已过，已删除云端文件：%s", d.Path), "")
		if row, _ := a.DB.FindFileByStrmPath(d.StrmPath); row != nil {
			_ = a.DB.DeleteFileRow(row.ID)
		}
	}
	a.Event("info", "link", fmt.Sprintf("联动删除对账完成：清理本地 %d，清理云端 %d", totalCloud, totalLocal), "")
}

func (a *App) hasPendingDeletion(fileID string) bool {
	for _, d := range a.DB.ListDeletions() {
		if d.FileID == fileID && d.State == "pending" {
			return true
		}
	}
	return false
}

// ConfirmDeletion 手动确认一条待删（宽限期内可以反悔）。
func (a *App) ConfirmDeletion(id int64) error {
	d, err := a.DB.GetDeletion(id)
	if err != nil || d == nil {
		return fmt.Errorf("记录不存在")
	}
	client, err := a.client(d.AccountID)
	if err != nil {
		return err
	}
	if err := client.Delete(context.Background(), []string{d.FileID}); err != nil {
		return err
	}
	_ = a.DB.UpdateDeletion(id, map[string]any{"state": "done"})
	a.Event("info", "link", fmt.Sprintf("已手动确认删除云端文件：%s", d.Path), "")
	return nil
}

// CancelDeletion 撤销一条待删（本地 strm 又回来了）。
func (a *App) CancelDeletion(id int64) error {
	return a.DB.DeleteDeletion(id)
}
