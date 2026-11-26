package service

import (
	"context"
	"encoding/json"
	"fmt"
	"path"
	"strings"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
	"panshare/internal/strm"
)

// Ed2kRequest 是添加 ed2k 链接的提交参数。
type Ed2kRequest struct {
	URLs          string `json:"urls"` // 多行，每行一条 ed2k://
	AccountID     string `json:"account_id"`
	DirCID        string `json:"dir_cid"`
	DirName       string `json:"dir_name"`
	LocalDir      string `json:"local_dir"`
	TransferOnAdd *bool  `json:"transfer_on_add"` // 不给就用全局设置
}

// AddEd2k 解析链接、建任务、立刻写 strm。
//
// 默认「到播放时才转存」：strm 先落地，Emby 能看到条目；真正点播放时
// /p115/ed2k/<hash> 才提交离线任务。也可以在添加时就转存。
func (a *App) AddEd2k(req Ed2kRequest) ([]model.Ed2kTask, error) {
	s := a.Settings()
	accountID := req.AccountID
	if accountID == "" {
		accountID = s.Ed2kAccountID
	}
	if accountID == "" {
		if acc := a.DB.DefaultAccount(); acc != nil {
			accountID = acc.ID
		}
	}
	if accountID == "" {
		return nil, fmt.Errorf("请先选择 115 账号")
	}
	onAdd := s.Ed2kTransferOnAdd
	if req.TransferOnAdd != nil {
		onAdd = *req.TransferOnAdd
	}
	dirCID := req.DirCID
	if dirCID == "" {
		dirCID = s.Ed2kDirCID
	}
	localDir := req.LocalDir
	if localDir == "" {
		// 页面上「固定」的本地目录优先，没设过才按月份分目录
		localDir = s.Ed2kLocalDir
	}
	if localDir == "" {
		localDir = path.Join("ed2k", time.Now().Format("2006-01"))
	}

	out := []model.Ed2kTask{}
	for _, line := range strings.Split(req.URLs, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || !strings.HasPrefix(strings.ToLower(line), "ed2k://") {
			continue
		}
		name, size, hash, err := p115.ParseEd2k(line)
		if err != nil {
			return out, err
		}
		if old, _ := a.DB.GetEd2k(hash); old != nil {
			out = append(out, *old)
			continue
		}
		task := model.Ed2kTask{
			ID: hash, Name: name, Size: size, URL: line, AccountID: accountID,
			DirCID: dirCID, LocalDir: localDir, TransferOnAdd: onAdd,
			State: "pending", CreatedAt: time.Now().Unix(),
		}
		if err := a.DB.SaveEd2k(task); err != nil {
			return out, err
		}
		a.writeEd2kStrm(task)
		out = append(out, task)
		if onAdd {
			go a.runEd2k(hash)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("没有识别到 ed2k 链接")
	}
	a.Event("info", "ed2k", fmt.Sprintf("已添加 %d 条 ed2k 任务%s", len(out),
		map[bool]string{true: "（添加时即转存）", false: "（播放时再转存）"}[onAdd]), "")
	return out, nil
}

func (a *App) writeEd2kStrm(task model.Ed2kTask) string {
	localDir := task.LocalDir
	if localDir == "" {
		localDir = path.Join("ed2k", time.Now().Format("2006-01"))
	}
	rel := path.Join(localDir, strm.Sanitize(task.Name, 180))
	abs, _, err := a.Store.Write(rel, a.Ed2kPlayURL(task.ID, task.Name))
	if err != nil {
		a.Event("warn", "ed2k", fmt.Sprintf("写 strm 失败：%s（%v）", task.Name, err), "")
		return ""
	}
	_ = a.DB.UpsertFile(model.StrmFile{
		ConfigID: "ed2k:" + task.ID, AccountID: task.AccountID, FileID: task.ID,
		Name: task.Name, RelPath: path.Join(localDir, strm.Sanitize(task.Name, 180)),
		StrmPath: abs, Size: task.Size, Source: "ed2k",
	})
	return abs
}

// runEd2k 立刻转存（添加时转存 / 播放时兜底都走它）。
func (a *App) runEd2k(hash string) {
	task, err := a.DB.GetEd2k(hash)
	if err != nil || task == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	_ = a.DB.UpdateEd2k(hash, map[string]any{"state": "transferring", "message": "提交离线任务…"})

	client, err := a.client(task.AccountID)
	if err != nil {
		a.ed2kFail(hash, err)
		return
	}
	dirCID := task.DirCID
	if dirCID == "" {
		if id, err := client.EnsureDir(ctx, "0", "ed2k"); err == nil {
			dirCID = id
			_ = a.DB.UpdateEd2k(hash, map[string]any{"dir_cid": dirCID})
		}
	}
	if err := client.OfflineAdd(ctx, task.URL, dirCID); err != nil {
		a.ed2kFail(hash, fmt.Errorf("提交离线任务失败：%w", err))
		return
	}
	_ = a.DB.UpdateEd2k(hash, map[string]any{"message": "115 正在下载…"})

	// 轮询任务状态
	for {
		select {
		case <-ctx.Done():
			a.ed2kFail(hash, fmt.Errorf("转存超时"))
			return
		case <-time.After(5 * time.Second):
		}
		tasks, err := client.OfflineList(ctx)
		if err != nil {
			continue
		}
		var cur *p115.OfflineTask
		for i := range tasks {
			if tasks[i].InfoHash == hash {
				cur = &tasks[i]
				break
			}
		}
		if cur == nil {
			// 列表里没这条时再单查一次：新任务有时还没进列表分页
			if t, err := client.OfflineTaskInfo(ctx, hash); err == nil && t != nil {
				cur = t
			}
		}
		if cur == nil {
			continue
		}
		switch cur.Status {
		case "done":
			node, err := client.WaitFile(ctx, dirCID, task.Name, 60*time.Second)
			if err != nil || node == nil {
				// 115 有时会把文件名改掉（非法字符/超长截断），按大小兜底找一次
				if n2, e2 := client.FindBySize(ctx, dirCID, task.Size); e2 == nil && n2 != nil {
					node, err = n2, nil
				}
			}
			if err != nil || node == nil {
				a.ed2kFail(hash, fmt.Errorf("转存完成但没找到文件：%v", err))
				return
			}
			pickcode := node.Pickcode
			if pickcode == "" {
				if d, err := client.Details(ctx, node.ID); err == nil {
					pickcode = d.Pickcode
				}
			}
			_ = a.DB.UpdateEd2k(hash, map[string]any{
				"state": "done", "message": "已转存", "pickcode": pickcode, "file_id": node.ID,
			})
			if abs := a.writeEd2kStrm(*task); abs != "" {
				_ = a.DB.UpsertFile(model.StrmFile{
					ConfigID: "ed2k:" + hash, AccountID: task.AccountID, FileID: node.ID,
					Pickcode: pickcode, Name: task.Name,
					RelPath:  path.Join(task.LocalDir, strm.Sanitize(task.Name, 180)),
					StrmPath: abs, Size: task.Size, Source: "ed2k",
				})
			}
			a.Event("success", "ed2k", fmt.Sprintf("ed2k 已转存：%s", task.Name), "")
			return
		case "failed":
			a.ed2kFail(hash, fmt.Errorf("115 离线下载失败：%s", cur.StatusTxt))
			return
		default:
			_ = a.DB.UpdateEd2k(hash, map[string]any{
				"message": fmt.Sprintf("115 下载中 %d%%", cur.Percent),
			})
		}
	}
}

func (a *App) ed2kFail(hash string, err error) {
	_ = a.DB.UpdateEd2k(hash, map[string]any{"state": "failed", "message": err.Error()})
	a.Event("error", "ed2k", fmt.Sprintf("ed2k 转存失败：%v", err), "")
}

// EnsureEd2k 播放时确保文件已转存，返回 pickcode。
func (a *App) EnsureEd2k(ctx context.Context, hash string) (string, *model.Ed2kTask, error) {
	task, err := a.DB.GetEd2k(hash)
	if err != nil || task == nil {
		return "", nil, fmt.Errorf("没有这条 ed2k 任务")
	}
	if task.Pickcode != "" {
		return task.Pickcode, task, nil
	}
	client, err := a.client(task.AccountID)
	if err != nil {
		return "", task, err
	}
	dirCID := task.DirCID
	if dirCID == "" {
		if id, err := client.EnsureDir(ctx, "0", "ed2k"); err == nil {
			dirCID = id
		}
	}
	// 文件可能已经转存过（换设备恢复索引后 pickcode 丢了），先按名字找
	node, _ := client.FindByName(ctx, dirCID, task.Name)
	if node == nil {
		node, _ = client.FindBySize(ctx, dirCID, task.Size)
	}
	if node != nil && !node.IsDir {
		pickcode := node.Pickcode
		if pickcode == "" {
			if d, err := client.Details(ctx, node.ID); err == nil {
				pickcode = d.Pickcode
			}
		}
		if pickcode != "" {
			_ = a.DB.UpdateEd2k(hash, map[string]any{"pickcode": pickcode, "file_id": node.ID, "state": "done"})
			return pickcode, task, nil
		}
	}
	// 还没转存：提交任务并等一小会儿（播放器会自动重试）
	go a.runEd2k(hash)
	deadline := time.Now().Add(75 * time.Second)
	for time.Now().Before(deadline) {
		t, _ := a.DB.GetEd2k(hash)
		if t != nil && t.Pickcode != "" {
			return t.Pickcode, t, nil
		}
		select {
		case <-ctx.Done():
			return "", task, ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
	return "", task, fmt.Errorf("115 还在转存这个 ed2k，请稍后重试")
}

// ExportEd2k 导出任务（JSON，换设备时导入即可恢复）。
func (a *App) ExportEd2k() []byte {
	tasks := a.DB.ListEd2k()
	data, _ := json.MarshalIndent(map[string]any{
		"version": 1, "exported_at": time.Now().Format(time.RFC3339), "tasks": tasks,
	}, "", "  ")
	return data
}

// ImportEd2k 导入任务：支持导出的 JSON，也支持纯 ed2k 链接文本。
func (a *App) ImportEd2k(data []byte, accountID, dirCID, localDir string) (int, error) {
	text := strings.TrimSpace(string(data))
	var req Ed2kRequest
	req.AccountID, req.DirCID, req.LocalDir = accountID, dirCID, localDir
	if strings.HasPrefix(text, "{") {
		var payload struct {
			Tasks []model.Ed2kTask `json:"tasks"`
		}
		if err := json.Unmarshal([]byte(text), &payload); err != nil {
			return 0, fmt.Errorf("导入文件不是合法的 JSON：%w", err)
		}
		n := 0
		for _, t := range payload.Tasks {
			if t.URL == "" && t.ID != "" {
				continue
			}
			name, size, hash, err := p115.ParseEd2k(t.URL)
			if err != nil {
				continue
			}
			if old, _ := a.DB.GetEd2k(hash); old != nil {
				continue
			}
			task := model.Ed2kTask{
				ID: hash, Name: firstNonEmptyStr(t.Name, name), Size: size, URL: t.URL,
				AccountID: firstNonEmptyStr(accountID, t.AccountID), DirCID: firstNonEmptyStr(dirCID, t.DirCID),
				LocalDir: firstNonEmptyStr(localDir, t.LocalDir), State: "pending",
				CreatedAt: time.Now().Unix(),
			}
			if task.AccountID == "" {
				continue
			}
			if err := a.DB.SaveEd2k(task); err != nil {
				continue
			}
			a.writeEd2kStrm(task)
			n++
		}
		a.Event("info", "ed2k", fmt.Sprintf("已从备份导入 %d 条 ed2k 任务", n), "")
		return n, nil
	}
	req.URLs = text
	tasks, err := a.AddEd2k(req)
	return len(tasks), err
}

// DeleteEd2k 删除任务；可选同时删云端文件与本地 strm。
func (a *App) DeleteEd2k(hash string, deleteCloud, deleteLocal bool) error {
	task, err := a.DB.GetEd2k(hash)
	if err != nil || task == nil {
		return fmt.Errorf("任务不存在")
	}
	if deleteCloud && task.FileID != "" {
		if client, err := a.client(task.AccountID); err == nil {
			_ = client.Delete(context.Background(), []string{task.FileID})
		}
	}
	if deleteLocal {
		for _, r := range a.DB.ListFiles("ed2k:" + hash) {
			if r.StrmPath != "" {
				_, _ = a.Store.RemoveAbs(r.StrmPath, a.Settings().DeleteMetaWithStrm)
			}
			_ = a.DB.DeleteFileRow(r.ID)
		}
	}
	if err := a.DB.DeleteEd2k(hash); err != nil {
		return err
	}
	a.Event("info", "ed2k", fmt.Sprintf("已删除 ed2k 任务：%s", task.Name), "")
	return nil
}
