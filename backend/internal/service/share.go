package service

import (
	"context"
	"fmt"
	"path"
	"strings"
	"sync"
	"time"

	"panshare/internal/model"
	"panshare/internal/p115"
	"panshare/internal/strm"
)

// ShareRequest 是「分享转存」页面的提交参数。
type ShareRequest struct {
	URL        string `json:"url"`
	AccountID  string `json:"account_id"`
	DirCID     string `json:"dir_cid"`
	DirName    string `json:"dir_name"`
	LocalDir   string `json:"local_dir"`
	Name       string `json:"name"`
	NoTransfer bool   `json:"no_transfer"`
}

var shareLocks sync.Map // taskID -> *sync.Mutex

// CreateShare 建一个转存任务并异步执行。
func (a *App) CreateShare(req ShareRequest) (*model.ShareTask, error) {
	link, err := p115.ParseShare(req.URL)
	if err != nil {
		return nil, err
	}
	accountID := req.AccountID
	if accountID == "" {
		acc := a.DB.DefaultAccount()
		if acc == nil {
			return nil, fmt.Errorf("还没有 115 账号")
		}
		accountID = acc.ID
	}
	task := model.ShareTask{
		ID: fmt.Sprintf("s%d", time.Now().UnixNano()), AccountID: accountID,
		URL: strings.TrimSpace(req.URL), ShareCode: link.ShareCode,
		ReceiveCode: link.ReceiveCode, DirCID: req.DirCID, DirName: req.DirName,
		LocalDir: req.LocalDir, NoTransfer: req.NoTransfer, Title: req.Name,
		Status: "pending", CreatedAt: time.Now().Unix(),
	}
	if err := a.DB.SaveShare(task); err != nil {
		return nil, err
	}
	go a.runShare(task.ID)
	return &task, nil
}

func (a *App) runShare(id string) {
	task, err := a.DB.GetShare(id)
	if err != nil || task == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Hour)
	defer cancel()
	_ = a.DB.UpdateShare(id, map[string]any{"status": "running", "message": "读取分享内容…"})
	a.Event("info", "share", fmt.Sprintf("开始处理分享：%s", task.URL), "")

	fail := func(err error) {
		_ = a.DB.UpdateShare(id, map[string]any{"status": "error", "message": err.Error()})
		a.Event("error", "share", fmt.Sprintf("分享转存失败：%v", err), task.URL)
	}

	client, err := a.client(task.AccountID)
	if err != nil {
		fail(err)
		return
	}
	tree, err := client.ListShareTree(ctx, task.ShareCode, task.ReceiveCode)
	if err != nil {
		fail(fmt.Errorf("读取分享内容失败：%w", err))
		return
	}
	if tree.FileNum == 0 && tree.DirNum == 0 {
		fail(fmt.Errorf("分享里没有内容（提取码错误或分享已失效）"))
		return
	}
	title := firstNonEmptyStr(task.Title, tree.Title, "分享-"+task.ShareCode)
	localDir := task.LocalDir
	if localDir == "" {
		localDir = a.Settings().ShareLocalDir
	}
	if localDir == "" {
		localDir = path.Join("分享", strm.Sanitize(title, 80))
	}
	_ = a.DB.UpdateShare(id, map[string]any{
		"title": title, "local_dir": localDir, "files": tree.FileNum,
		"message": fmt.Sprintf("共 %d 个文件 / %d 个目录，开始%s…", tree.FileNum, tree.DirNum,
			map[bool]string{true: "直接生成", false: "转存"}[task.NoTransfer]),
	})

	written := 0
	if task.NoTransfer {
		// 不转存：直接用源分享通道生成，播放时从源分享解析直链
		for _, item := range tree.Items {
			if item.IsDir {
				continue
			}
			rel := path.Join(localDir, item.RelPath)
			u := a.SharePlayURL(task.ShareCode, item.ID, item.Name, task.ReceiveCode)
			abs, _, err := a.Store.Write(rel, u)
			if err != nil {
				continue
			}
			written++
			_ = a.DB.UpsertFile(model.StrmFile{
				ConfigID: "share:" + id, AccountID: task.AccountID, FileID: item.ID,
				Name: item.Name, RelPath: path.Join(localDir, item.RelPath),
				StrmPath: abs, Size: item.Size, Sha1: item.Sha1,
				ShareCode: task.ShareCode, Source: "share-nc",
			})
		}
		_ = a.DB.UpdateShare(id, map[string]any{
			"status": "done", "files": written,
			"message": fmt.Sprintf("已直接生成 %d 个 strm（未转存，播放时走源分享）", written),
		})
		a.Event("success", "share", fmt.Sprintf("分享已直接生成：%s（%d 个 strm）", title, written), "")
		return
	}

	// 转存到接收目录
	dirCID := task.DirCID
	if dirCID == "" {
		s := a.Settings()
		dirCID = s.ShareDirCID
	}
	if dirCID == "" {
		root, err := client.EnsureDir(ctx, "0", "分享接收")
		if err != nil {
			fail(fmt.Errorf("创建接收目录失败：%w", err))
			return
		}
		dirCID = root
		_ = a.DB.UpdateShare(id, map[string]any{"dir_cid": dirCID, "dir_name": "分享接收"})
	}
	if err := client.ShareReceive(ctx, task.ShareCode, task.ReceiveCode, tree.TopIDs, dirCID); err != nil {
		fail(fmt.Errorf("转存失败：%w", err))
		return
	}

	// 转存后接收目录里的顶层条目
	topNodes, err := client.ListDir(ctx, dirCID)
	if err != nil {
		fail(fmt.Errorf("读取接收目录失败：%w", err))
		return
	}
	resolver := newPathResolver(client, dirCID, topNodes)

	// 用自己的文件建长期分享
	topIDs := make([]string, 0, len(tree.TopIDs))
	topNames := map[string]bool{}
	for _, item := range tree.Items {
		first := strings.Split(item.RelPath, "/")[0]
		topNames[first] = true
	}
	for _, n := range topNodes {
		if topNames[n.Name] {
			topIDs = append(topIDs, n.ID)
		}
	}
	if len(topIDs) == 0 {
		fail(fmt.Errorf("转存后在接收目录里没找到对应条目"))
		return
	}
	myCode, myRC, err := client.ShareSend(ctx, topIDs)
	if err != nil {
		fail(fmt.Errorf("创建自己的分享失败：%w", err))
		return
	}
	// 设成长期分享（-1 天 = 永久）
	if myRC != "" {
		if err := client.ShareUpdate(ctx, myCode, myRC, -1); err != nil {
			a.Event("warn", "share", fmt.Sprintf("分享 %s 设为长期失败（仍可用，但会到期）：%v", myCode, err), "")
		}
	}
	_ = a.DB.UpdateShare(id, map[string]any{"my_share_code": myCode, "my_receive_code": myRC})

	for _, item := range tree.Items {
		if item.IsDir {
			continue
		}
		node, err := resolver.resolve(ctx, item.RelPath)
		if err != nil || node == nil {
			continue
		}
		rel := path.Join(localDir, item.RelPath)
		u := a.SharePlayURL(myCode, node.ID, item.Name, myRC)
		abs, _, err := a.Store.Write(rel, u)
		if err != nil {
			continue
		}
		written++
		_ = a.DB.UpsertFile(model.StrmFile{
			ConfigID: "share:" + id, AccountID: task.AccountID, FileID: node.ID,
			Pickcode: node.Pickcode, Name: item.Name,
			RelPath: path.Join(localDir, item.RelPath), StrmPath: abs, Size: item.Size,
			Sha1: item.Sha1, ShareCode: myCode, Source: "share",
		})
	}
	_ = a.DB.UpdateShare(id, map[string]any{
		"status": "done", "files": written,
		"message": fmt.Sprintf("已转存并创建长期分享 %s，生成 %d 个 strm", myCode, written),
	})
	a.Event("success", "share", fmt.Sprintf("分享转存完成：%s · %d 个 strm · 自己的分享 %s", title, written, myCode), "")
}

// pathResolver 在接收目录里按相对路径找节点，缓存每级目录列表。
type pathResolver struct {
	client *p115.Client
	root   string
	cache  map[string][]p115.Node
}

func newPathResolver(client *p115.Client, root string, top []p115.Node) *pathResolver {
	return &pathResolver{client: client, root: root, cache: map[string][]p115.Node{root: top}}
}

func (r *pathResolver) list(ctx context.Context, cid string) []p115.Node {
	if nodes, ok := r.cache[cid]; ok {
		return nodes
	}
	nodes, err := r.client.ListDir(ctx, cid)
	if err != nil {
		nodes = nil
	}
	r.cache[cid] = nodes
	return nodes
}

func (r *pathResolver) resolve(ctx context.Context, rel string) (*p115.Node, error) {
	parts := strings.Split(strings.Trim(rel, "/"), "/")
	cur := r.root
	for i, name := range parts {
		var found *p115.Node
		for _, n := range r.list(ctx, cur) {
			if n.Name == name {
				cp := n
				found = &cp
				break
			}
		}
		if found == nil {
			return nil, fmt.Errorf("接收目录里找不到 %s", rel)
		}
		if i == len(parts)-1 {
			return found, nil
		}
		if !found.IsDir {
			return nil, fmt.Errorf("%s 不是目录", name)
		}
		cur = found.ID
	}
	return nil, fmt.Errorf("空路径")
}

// DeleteShare 删除分享任务：可选取消自己的分享、删掉云端文件、删本地 strm。
func (a *App) DeleteShare(id string, cancelShare, deleteCloud, deleteLocal bool) error {
	task, err := a.DB.GetShare(id)
	if err != nil || task == nil {
		return fmt.Errorf("任务不存在")
	}
	client, _ := a.client(task.AccountID)
	if cancelShare && task.MyShareCode != "" && client != nil {
		_ = client.ShareDelete(context.Background(), task.MyShareCode)
	}
	if deleteCloud && client != nil && task.DirCID != "" {
		if nodes, err := client.ListDir(context.Background(), task.DirCID); err == nil {
			names := map[string]bool{}
			for _, r := range a.DB.ListFiles("share:" + id) {
				names[strings.Split(r.RelPath, "/")[0]] = true
			}
			ids := make([]string, 0, len(nodes))
			for _, n := range nodes {
				if names[n.Name] {
					ids = append(ids, n.ID)
				}
			}
			if len(ids) > 0 {
				_ = client.Delete(context.Background(), ids)
			}
		}
	}
	if deleteLocal {
		for _, r := range a.DB.ListFiles("share:" + id) {
			if r.StrmPath != "" {
				_, _ = a.Store.RemoveAbs(r.StrmPath, a.Settings().DeleteMetaWithStrm)
			}
			_ = a.DB.DeleteFileRow(r.ID)
		}
	}
	for _, r := range a.DB.ListFiles("share:" + id) {
		_ = a.DB.DeleteFileRow(r.ID)
	}
	if err := a.DB.DeleteShare(id); err != nil {
		return err
	}
	a.Event("info", "share", fmt.Sprintf("已删除分享任务：%s", task.Title), "")
	return nil
}
