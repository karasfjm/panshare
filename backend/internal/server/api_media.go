package server

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"panshare/internal/model"
	"panshare/internal/service"
)

// ---------------------------------------------------------------- Emby

func (s *Server) listEmby(w http.ResponseWriter, r *http.Request) {
	servers := s.app.ListEmbyServers()
	out := make([]map[string]any, 0, len(servers))
	for _, v := range servers {
		out = append(out, map[string]any{
			"id": v.ID, "name": v.Name, "url": v.URL, "enabled": v.Enabled,
			"is_default": v.IsDefault, "has_key": v.APIKey != "", "created_at": v.CreatedAt,
		})
	}
	writeJSON(w, map[string]any{"servers": out})
}

func (s *Server) saveEmby(w http.ResponseWriter, r *http.Request) {
	var v model.EmbyServer
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if v.ID == "" {
		v.ID = "e" + time.Now().Format("20060102150405")
	}
	if err := s.app.SaveEmbyServer(v); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "id": v.ID})
}

func (s *Server) deleteEmby(w http.ResponseWriter, r *http.Request) {
	if err := s.app.DeleteEmbyServer(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) testEmby(w http.ResponseWriter, r *http.Request) {
	var v model.EmbyServer
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	info, err := s.app.TestEmby(r.Context(), v)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "info": info})
}

func (s *Server) embyUsers(w http.ResponseWriter, r *http.Request) {
	users, err := s.app.EmbyUsers(r.Context(), r.URL.Query().Get("server_id"))
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"users": users})
}

// embyProxyInfo 给前端展示反代地址与状态（端口是环境变量，前端拿不到）。
func (s *Server) embyProxyInfo(w http.ResponseWriter, r *http.Request) {
	info := s.app.Proxy.Stats()
	if srv := s.app.UpstreamEmby(); srv != nil {
		info["server_name"] = srv.Name
		info["server_url"] = srv.URL
	}
	info["base_url"] = s.app.BaseURL()
	writeJSON(w, info)
}

// saveEmbyProxy 改反代监听端口 / 开关：port=0 表示回到环境变量里的默认端口，
// enabled=false 表示整体关掉（端口留着，下次打开还是这个端口）。
func (s *Server) saveEmbyProxy(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Port    *int  `json:"port"`
		Enabled *bool `json:"enabled"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if body.Port != nil && (*body.Port < 0 || *body.Port > 65535) {
		writeErr(w, http.StatusBadRequest, errText("端口要在 0-65535 之间"))
		return
	}
	st := s.app.Settings()
	if body.Port != nil {
		st.EmbyProxyPort = *body.Port
	}
	if body.Enabled != nil {
		st.EmbyProxyOff = !*body.Enabled
	}
	if err := s.app.SaveSettings(st); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	// 监听重绑是异步的，等它落定再返回，前端刷新一次就能看到新端口
	want := s.app.Proxy.EffectivePort()
	for i := 0; i < 30; i++ {
		if want <= 0 {
			if !s.app.Proxy.Running() {
				break
			}
		} else if s.app.Proxy.Running() && s.app.Proxy.ListenPort() == want {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	writeJSON(w, map[string]any{"ok": true, "stats": s.app.Proxy.Stats()})
}

// ---------------------------------------------------------------- 设置 / 状态

func (s *Server) getSettings(w http.ResponseWriter, r *http.Request) {
	st := s.app.Settings()
	st.StrmRoot = s.app.Store.Root
	writeJSON(w, map[string]any{
		"settings": st,
		"derived": map[string]any{
			"strm_root": s.app.Store.Root,
			"base_url":  s.app.BaseURL(),
			"uptime":    time.Since(s.app.StartedAt()).Round(time.Second).String(),
			"version":   Version,
		},
	})
}

func (s *Server) saveSettings(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	st, err := s.mergeSettings(body)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.app.SaveSettings(st); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	s.app.Event("info", "settings", "服务设置已保存", "")
	writeJSON(w, map[string]any{"ok": true})
}

// mergeSettings 把请求体叠在当前设置上：只覆盖请求里出现过的键。
//
// 前端是「读整份 → 改几项 → 整份写回」，但直接用裸 json.Decode 进 Settings 的话，
// 任何一处漏传字段（或客户端形状写错，比如多包了一层 settings）都会把别的开关
// 静默清成零值，代价太大的配置（映射根、TG、反代端口）不能这么丢。
func (s *Server) mergeSettings(body []byte) (model.Settings, error) {
	var patch map[string]json.RawMessage
	if err := json.Unmarshal(body, &patch); err != nil {
		return model.Settings{}, err
	}
	// 容忍 {"settings": {...}} 这种包一层的写法
	if inner, ok := patch["settings"]; ok && len(inner) > 0 && inner[0] == '{' {
		var nested map[string]json.RawMessage
		if err := json.Unmarshal(inner, &nested); err != nil {
			return model.Settings{}, err
		}
		patch = nested
	}
	if len(patch) == 0 {
		return s.app.Settings(), nil
	}

	merged := map[string]json.RawMessage{}
	cur, err := json.Marshal(s.app.Settings())
	if err != nil {
		return model.Settings{}, err
	}
	if err := json.Unmarshal(cur, &merged); err != nil {
		return model.Settings{}, err
	}
	for k, v := range patch {
		merged[k] = v
	}
	out, err := json.Marshal(merged)
	if err != nil {
		return model.Settings{}, err
	}
	st := s.app.Settings()
	if err := json.Unmarshal(out, &st); err != nil {
		return model.Settings{}, err
	}
	return st, nil
}

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"events": s.app.DB.ListEvents(300)})
}

func (s *Server) clearEvents(w http.ResponseWriter, r *http.Request) {
	if err := s.app.DB.ClearEvents(); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) stats(w http.ResponseWriter, r *http.Request) {
	accs := s.app.DB.ListAccounts()
	cfgs := s.app.DB.ListConfigs()
	files := 0
	running := 0
	for _, c := range cfgs {
		files += s.app.DB.CountFiles(c.ID)
		if s.app.IsRunning(c.ID) {
			running++
		}
	}
	online := 0
	for _, a := range accs {
		if a.Status == "ok" {
			online++
		}
	}
	ed2kDone := 0
	for _, t := range s.app.DB.ListEd2k() {
		if t.State == "done" {
			ed2kDone++
		}
	}
	writeJSON(w, map[string]any{
		"accounts": len(accs), "accounts_ok": online,
		"configs": len(cfgs), "configs_running": running,
		"files": files, "shares": len(s.app.DB.ListShares()),
		"ed2k": len(s.app.DB.ListEd2k()), "ed2k_done": ed2kDone,
		"emby_servers": len(s.app.DB.ListEmbyServers()),
		"emby_proxy":   s.app.Proxy.Stats(),
		"strm_root": s.app.Store.Root, "base_url": s.app.BaseURL(),
		"uptime": time.Since(s.app.StartedAt()).Round(time.Second).String(),
		"version": Version, "settings": s.app.Settings(),
		"events": s.app.DB.ListEvents(20),
	})
}

// ---------------------------------------------------------------- 联动删除

func (s *Server) listDeletions(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"deletions": s.app.DB.ListDeletions()})
}

func (s *Server) linkScan(w http.ResponseWriter, r *http.Request) {
	if !s.app.Settings().DeleteEnabled {
		writeErr(w, http.StatusBadRequest, errText("联动删除总开关没开"))
		return
	}
	go s.app.LinkScan(r.Context())
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) confirmDeletion(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var n int64
	if _, err := fmtSscan(id, &n); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.app.ConfirmDeletion(n); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) cancelDeletion(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var n int64
	if _, err := fmtSscan(id, &n); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.app.CancelDeletion(n); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

// ---------------------------------------------------------------- Telegram / 插件

func (s *Server) testTG(w http.ResponseWriter, r *http.Request) {
	if !s.app.Settings().TGEnabled {
		writeErr(w, http.StatusBadRequest, errText("请先开启 Telegram 通知并填好 Bot Token 与 Chat ID"))
		return
	}
	// 同步发：失败就把 Telegram 的原话带回去，别让页面显示「已发出」实际没到
	if err := s.app.TG.Test(); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) tgStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.app.TG.Status())
}

func (s *Server) listPlugins(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"plugins": s.app.Plugins.List()})
}

func (s *Server) installPlugin(w http.ResponseWriter, r *http.Request) {
	if err := s.app.Plugins.Install(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) startPlugin(w http.ResponseWriter, r *http.Request) {
	if err := s.app.Plugins.Start(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) stopPlugin(w http.ResponseWriter, r *http.Request) {
	if err := s.app.Plugins.Stop(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) uninstallPlugin(w http.ResponseWriter, r *http.Request) {
	if err := s.app.Plugins.Uninstall(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

// pluginProxy 把 /api/plugins/<id>/proxy/xxx 转给插件进程。
func (s *Server) pluginProxy(w http.ResponseWriter, r *http.Request) {
	if !s.valid(r) {
		writeErr(w, http.StatusUnauthorized, errText("未登录"))
		return
	}
	id := r.PathValue("id")
	h, ok := s.app.Plugins.Handler(id)
	if !ok {
		writeErr(w, http.StatusServiceUnavailable, errText("插件没有运行"))
		return
	}
	prefix := "/api/plugins/" + id + "/proxy"
	clone := r.Clone(r.Context())
	clone.URL.Path = strings.TrimPrefix(r.URL.Path, prefix)
	if clone.URL.Path == "" {
		clone.URL.Path = "/"
	}
	h.ServeHTTP(w, clone)
}

// ---------------------------------------------------------------- 小工具

type errText string

func (e errText) Error() string { return string(e) }

func fmtSscan(v string, out *int64) (int, error) {
	var n int64
	for _, ch := range v {
		if ch < '0' || ch > '9' {
			return 0, errText("非法的 id")
		}
		n = n*10 + int64(ch-'0')
	}
	*out = n
	return 1, nil
}

var _ = service.ModeInc
