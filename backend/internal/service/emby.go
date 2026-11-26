package service

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"panshare/internal/model"
)

// 这里只放「Emby 服务器」这一层：登记地址 / API Key、测连通、读用户列表。
// 秒传用户映射、联动删除都依赖它；播放本身由 /p115/play 直出，不再做反代。

func (a *App) ListEmbyServers() []model.EmbyServer { return a.DB.ListEmbyServers() }

func (a *App) SaveEmbyServer(s model.EmbyServer) error {
	s.URL = strings.TrimRight(strings.TrimSpace(s.URL), "/")
	if s.Name == "" || s.URL == "" {
		return fmt.Errorf("名称与地址都要填")
	}
	if err := a.DB.SaveEmbyServer(s); err != nil {
		return err
	}
	// 反代缓存的「默认服务器」立刻失效，否则改了地址还要等 5 秒
	a.Proxy.InvalidateTarget()
	return nil
}

func (a *App) DeleteEmbyServer(id string) error {
	if err := a.DB.DeleteEmbyServer(id); err != nil {
		return err
	}
	a.Proxy.InvalidateTarget()
	return nil
}

// UpstreamEmby 返回第一台启用的服务器，没显式指定 server_id 时当默认值。
func (a *App) UpstreamEmby() *model.EmbyServer {
	for _, s := range a.DB.ListEmbyServers() {
		if s.Enabled {
			return &s
		}
	}
	return nil
}

// TestEmby 用 /System/Info 验证地址与 API Key。
func (a *App) TestEmby(ctx context.Context, s model.EmbyServer) (map[string]any, error) {
	base := strings.TrimRight(s.URL, "/")
	if base == "" {
		return nil, fmt.Errorf("地址不能为空")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/System/Info", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Emby-Token", s.APIKey)
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, fmt.Errorf("连不上 Emby：%w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("Emby 返回 HTTP %d（API Key 可能不对）", resp.StatusCode)
	}
	var info map[string]any
	_ = json.Unmarshal(body, &info)
	return info, nil
}

// EmbyUsers 读 Emby 用户列表，给「秒传用户映射」用。
func (a *App) EmbyUsers(ctx context.Context, serverID string) ([]map[string]any, error) {
	var srv *model.EmbyServer
	if serverID != "" {
		srv, _ = a.DB.GetEmbyServer(serverID)
	}
	if srv == nil {
		srv = a.UpstreamEmby()
	}
	if srv == nil {
		return nil, fmt.Errorf("请先添加一台 Emby 服务器")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(srv.URL, "/")+"/Users", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Emby-Token", srv.APIKey)
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var users []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&users); err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(users))
	for _, u := range users {
		id, _ := u["Id"].(string)
		name, _ := u["Name"].(string)
		if id == "" {
			continue
		}
		out = append(out, map[string]any{"id": id, "name": name})
	}
	return out, nil
}
