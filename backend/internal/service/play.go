package service

import (
	"context"
	"fmt"
	"time"

	"panshare/internal/model"
)

// ResolvePlay 把一个 pickcode 解析成 115 直链（带同播复制）。
//
// ua 必须原样透传：115 直链要求「取链接的 UA」和「播 CDN 的 UA」一致。
func (a *App) ResolvePlay(ctx context.Context, pickcode, uid, ip, ua string) (string, map[string]string, error) {
	client, accountID, err := a.clientByUID(uid)
	if err != nil {
		return "", nil, err
	}
	useCopy := false
	if acc, _ := a.DB.GetAccount(accountID); acc != nil {
		useCopy = acc.CopyPlay
	}
	pickcode = a.ResolvePickcode(ctx, accountID, useCopy, pickcode, DeviceKey(ip, ua))
	link, header, err := client.DownloadURL(ctx, pickcode, ua, 8*time.Minute)
	if err != nil {
		return "", nil, err
	}
	return link, header, nil
}

// ResolvePlayInAccount 在**指定账号**里解析直链（秒传路由用）。
//
// 秒传后的文件在目标账号的网盘里，它的 pickcode 只有那个账号认得，
// 所以不能按 115 uid 去找（uid 可能是源账号的）。
func (a *App) ResolvePlayInAccount(ctx context.Context, accountID, pickcode, ip, ua string) (string, map[string]string, error) {
	client, err := a.client(accountID)
	if err != nil {
		return "", nil, err
	}
	useCopy := false
	if acc, _ := a.DB.GetAccount(accountID); acc != nil {
		useCopy = acc.CopyPlay
	}
	pickcode = a.ResolvePickcode(ctx, accountID, useCopy, pickcode, DeviceKey(ip, ua))
	link, header, err := client.DownloadURL(ctx, pickcode, ua, 8*time.Minute)
	if err != nil {
		return "", nil, err
	}
	return link, header, nil
}

// ResolveSharePlay 从分享通道解析直链（strm 指向自己的长期分享）。
func (a *App) ResolveSharePlay(ctx context.Context, shareCode, fileID, receiveCode string) (string, error) {
	task, _ := a.ShareByCode(shareCode)
	rc := receiveCode
	accountID := ""
	if task != nil {
		accountID = task.AccountID
		if task.MyShareCode == shareCode {
			rc = firstNonEmptyStr(task.MyReceiveCode, rc)
		} else if rc == "" {
			rc = task.ReceiveCode
		}
	}
	client, err := a.client(accountID)
	if err != nil {
		return "", err
	}
	link, err := client.ShareDownloadURL(ctx, shareCode, rc, fileID)
	if err != nil {
		return "", fmt.Errorf("分享直链解析失败：%w", err)
	}
	return link, nil
}

// ResolveEd2kPlay 播放 ed2k 时的直链（必要时先转存）。
func (a *App) ResolveEd2kPlay(ctx context.Context, hash, ua string) (string, error) {
	pickcode, task, err := a.EnsureEd2k(ctx, hash)
	if err != nil {
		return "", err
	}
	client, err := a.client(task.AccountID)
	if err != nil {
		return "", err
	}
	link, _, err := client.DownloadURL(ctx, pickcode, ua, 8*time.Minute)
	return link, err
}

// ShareByCode 按「自己的分享码」或「源分享码」找任务。
func (a *App) ShareByCode(code string) (*model.ShareTask, error) {
	if t, err := a.DB.FindShareByMyCode(code); err == nil && t != nil {
		return t, nil
	}
	return a.DB.FindShareByCode(code)
}
