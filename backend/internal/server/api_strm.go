package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"panshare/internal/model"
	"panshare/internal/service"
)

type configBody struct {
	Name            string           `json:"name"`
	AccountID       string           `json:"account_id"`
	Paths           []model.StrmPath `json:"paths"`
	RemoteID        string           `json:"remote_id"`
	RemotePath      string           `json:"remote_path"`
	LocalDir        string           `json:"local_dir"`
	Mode            string           `json:"mode"`
	IncludeExts     string           `json:"include_exts"`
	MetaExts        string           `json:"meta_exts"`
	StrmBase        string           `json:"strm_base"`
	AllFiles        bool             `json:"all_files"`
	OverwriteMode   string           `json:"overwrite_mode"`
	ExcludeKeywords string           `json:"exclude_keywords"`
	MinFileSizeMB   int              `json:"min_file_size_mb"`
	SkipPending     bool             `json:"skip_pending"`
	PendingDirs     string           `json:"pending_dirs"`
	Concurrency     int              `json:"concurrency"`
	CronFull        string           `json:"cron_full"`
	WatchEnabled    bool             `json:"watch_enabled"`
	WatchDelaySec   int              `json:"watch_delay_sec"`
	WatchInterval   int              `json:"watch_interval"`
	SchedEnabled    bool             `json:"sched_enabled"`
	SchedMinutes    int              `json:"sched_minutes"`
	DownloadMeta    bool             `json:"download_meta"`
}

func (s *Server) listConfigs(w http.ResponseWriter, r *http.Request) {
	cfgs := s.app.DB.ListConfigs()
	// TG 专属指令的代号跟 /help 里那份必须一致，所以统一由 service 层算
	aliases := s.app.TGConfigAliases()
	out := make([]map[string]any, 0, len(cfgs))
	for _, c := range cfgs {
		name := ""
		if acc, _ := s.app.DB.GetAccount(c.AccountID); acc != nil {
			name = acc.Name
			if name == "" {
				name = acc.UserName
			}
		}
		out = append(out, map[string]any{
			"id": c.ID, "name": c.Name, "account_id": c.AccountID, "account_name": name,
			"paths":     c.EffectivePaths(),
			"remote_id": c.RemoteID, "remote_path": c.RemotePath, "local_dir": c.LocalDir,
			"mode": c.Mode, "include_exts": c.IncludeExts, "meta_exts": c.MetaExts,
			"strm_base": c.StrmBase, "all_files": c.AllFiles,
			"overwrite_mode": c.OverwriteMode, "exclude_keywords": c.ExcludeKeywords,
			"min_file_size_mb": c.MinFileSizeMB, "skip_pending": c.SkipPending,
			"pending_dirs": c.PendingDirs, "concurrency": c.Concurrency,
			"cron_full": c.CronFull, "cleanup_stale_count": c.CleanupStaleCount,
			"watch_enabled": c.WatchEnabled, "watch_delay_sec": c.WatchDelaySec,
			"watch_interval": c.WatchInterval, "last_check_at": c.LastCheckAt,
			"sched_enabled": c.SchedEnabled, "sched_minutes": c.SchedMinutes,
			"download_meta": c.DownloadMeta, "last_run_at": c.LastRunAt,
			"last_status": c.LastStatus, "last_message": c.LastMessage,
			"files": s.app.DB.CountFiles(c.ID), "running": s.app.IsRunning(c.ID),
			"tg_alias": aliases[c.ID],
		})
	}
	writeJSON(w, map[string]any{"configs": out})
}

// cleanPaths 去掉空行，本地目录归一化，网盘 id 补默认值。
func cleanPaths(in []model.StrmPath) []model.StrmPath {
	out := make([]model.StrmPath, 0, len(in))
	for _, p := range in {
		p.RemotePath = strings.TrimSpace(p.RemotePath)
		p.LocalDir = strings.TrimSpace(p.LocalDir)
		p.RemoteID = strings.TrimSpace(p.RemoteID)
		if p.RemotePath == "" && p.LocalDir == "" {
			continue
		}
		if p.RemoteID == "" {
			p.RemoteID = "0"
		}
		out = append(out, p)
	}
	return out
}

func (s *Server) createConfig(w http.ResponseWriter, r *http.Request) {
	var b configBody
	if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	paths := cleanPaths(b.Paths)
	if len(paths) == 0 && b.LocalDir != "" {
		paths = cleanPaths([]model.StrmPath{{RemoteID: b.RemoteID, RemotePath: b.RemotePath, LocalDir: b.LocalDir}})
	}
	if strings.TrimSpace(b.Name) == "" || b.AccountID == "" || len(paths) == 0 {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("任务名称、账号、网盘目录都要填"))
		return
	}
	for _, p := range paths {
		if p.LocalDir == "" {
			writeErr(w, http.StatusBadRequest, fmt.Errorf("每条网盘路径都要选一个本地目录"))
			return
		}
	}
	if err := s.requireAccount(b.AccountID); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if b.Mode == "" {
		b.Mode = "pickcode"
	}
	if b.OverwriteMode != "always" && b.OverwriteMode != "never" {
		b.OverwriteMode = "always"
	}
	c := model.StrmConfig{
		ID: fmt.Sprintf("c%d", time.Now().UnixNano()), Name: strings.TrimSpace(b.Name), AccountID: b.AccountID,
		Paths:       paths,
		RemoteID:    paths[0].RemoteID,
		RemotePath:  paths[0].RemotePath,
		LocalDir:    paths[0].LocalDir,
		Mode:        b.Mode,
		IncludeExts: b.IncludeExts, MetaExts: b.MetaExts, StrmBase: strings.TrimSpace(b.StrmBase),
		AllFiles:      b.AllFiles,
		OverwriteMode: b.OverwriteMode, ExcludeKeywords: b.ExcludeKeywords,
		MinFileSizeMB: b.MinFileSizeMB, SkipPending: b.SkipPending, PendingDirs: b.PendingDirs,
		Concurrency: b.Concurrency, CronFull: b.CronFull,
		WatchEnabled: b.WatchEnabled, WatchDelaySec: b.WatchDelaySec, WatchInterval: b.WatchInterval,
		SchedEnabled: b.SchedEnabled, SchedMinutes: b.SchedMinutes, DownloadMeta: b.DownloadMeta,
	}
	if err := s.app.DB.CreateConfig(c); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	s.app.Event("info", "strm", fmt.Sprintf("新增生成配置：%s（%d 条路径）", c.Name, len(paths)), "")
	writeJSON(w, map[string]any{"ok": true, "config": c})
}

func (s *Server) updateConfig(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	cur, _ := s.app.DB.GetConfig(id)
	if cur == nil {
		writeErr(w, http.StatusNotFound, fmt.Errorf("配置不存在"))
		return
	}
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if v, ok := body["account_id"]; ok {
		if err := s.requireAccount(fmt.Sprint(v)); err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
	}
	allowed := map[string]string{
		"name": "name", "account_id": "account_id", "remote_id": "remote_id",
		"remote_path": "remote_path", "local_dir": "local_dir", "mode": "mode",
		"include_exts": "include_exts", "meta_exts": "meta_exts", "strm_base": "strm_base",
		"all_files":      "all_files",
		"skip_pending":   "skip_pending",
		"overwrite_mode": "overwrite_mode", "exclude_keywords": "exclude_keywords",
		"pending_dirs": "pending_dirs", "cron_full": "cron_full",
		"min_file_size_mb": "min_file_size_mb", "concurrency": "concurrency",
		"watch_enabled": "watch_enabled", "watch_delay_sec": "watch_delay",
		"watch_interval": "watch_interval",
		"sched_enabled":  "sched_enabled", "sched_minutes": "sched_minutes",
		"download_meta": "download_meta",
	}
	fields := map[string]any{}
	for k, col := range allowed {
		v, ok := body[k]
		if !ok {
			continue
		}
		switch col {
		case "all_files", "watch_enabled", "sched_enabled", "download_meta", "skip_pending":
			b, _ := v.(bool)
			if b {
				fields[col] = 1
			} else {
				fields[col] = 0
			}
		case "watch_delay", "sched_minutes", "watch_interval", "min_file_size_mb", "concurrency":
			n, _ := v.(float64)
			fields[col] = int(n)
		case "strm_base":
			fields[col] = strings.TrimRight(strings.TrimSpace(fmt.Sprint(v)), "/")
		default:
			fields[col] = fmt.Sprint(v)
		}
	}
	if v, ok := fields["overwrite_mode"]; ok {
		mode := fmt.Sprint(v)
		if mode != "always" && mode != "never" {
			mode = "always"
		}
		fields["overwrite_mode"] = mode
	}
	// 路径映射整组替换；顺手把老字段同步成第一条，兼容只读老字段的地方
	if raw, ok := body["paths"]; ok {
		b, _ := json.Marshal(raw)
		var paths []model.StrmPath
		if err := json.Unmarshal(b, &paths); err != nil {
			writeErr(w, http.StatusBadRequest, fmt.Errorf("路径格式不对"))
			return
		}
		paths = cleanPaths(paths)
		if len(paths) == 0 {
			writeErr(w, http.StatusBadRequest, fmt.Errorf("至少要留一条网盘路径"))
			return
		}
		for _, p := range paths {
			if p.LocalDir == "" {
				writeErr(w, http.StatusBadRequest, fmt.Errorf("每条网盘路径都要选一个本地目录"))
				return
			}
		}
		pj, _ := json.Marshal(paths)
		fields["paths"] = string(pj)
		fields["remote_id"] = paths[0].RemoteID
		fields["remote_path"] = paths[0].RemotePath
		fields["local_dir"] = paths[0].LocalDir
	}
	if err := s.app.DB.UpdateConfig(id, fields); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	// 改了监控范围就把指纹清掉，下一轮按新范围重算，避免拿旧指纹比对
	if _, ok := fields["paths"]; ok {
		_ = s.app.DB.UpdateConfig(id, map[string]any{"last_sig": ""})
	}
	writeJSON(w, map[string]any{"ok": true})
}

// requireAccount 校验配置里选的账号确实存在，避免存下一条永远跑不起来的配置。
func (s *Server) requireAccount(id string) error {
	acc, err := s.app.DB.GetAccount(id)
	if err != nil {
		return err
	}
	if acc == nil {
		return fmt.Errorf("账号不存在，请先在「115 助手」里添加")
	}
	return nil
}

func (s *Server) deleteConfig(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	c, _ := s.app.DB.GetConfig(id)
	if c == nil {
		writeErr(w, http.StatusNotFound, fmt.Errorf("配置不存在"))
		return
	}
	if r.URL.Query().Get("keep_files") != "1" {
		for _, row := range s.app.DB.ListFiles(id) {
			if row.StrmPath != "" {
				_, _ = s.app.Store.RemoveAbs(row.StrmPath, s.app.Settings().DeleteMetaWithStrm)
			}
		}
	}
	if err := s.app.DB.DeleteConfig(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	s.app.Event("info", "strm", fmt.Sprintf("已删除生成配置：%s", c.Name), "")
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) runConfig(w http.ResponseWriter, r *http.Request) {
	mode := service.GenMode(r.URL.Query().Get("mode"))
	if mode == "" {
		mode = service.ModeInc
	}
	ok, err := s.app.RunStrm(r.PathValue("id"), mode)
	if err != nil {
		writeErr(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, map[string]any{"ok": ok, "mode": mode})
}

func (s *Server) configFiles(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	limit := 1000
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	rows := s.app.DB.ListFiles(id)
	if len(rows) > limit {
		rows = rows[:limit]
	}
	writeJSON(w, map[string]any{"files": rows, "total": s.app.DB.CountFiles(id)})
}

func (s *Server) searchFiles(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	if q == "" {
		writeJSON(w, map[string]any{"files": []any{}})
		return
	}
	writeJSON(w, map[string]any{"files": s.app.DB.SearchFiles(q, 200)})
}

// ---------------------------------------------------------------- 清理确认队列

func (s *Server) listCleanups(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"cleanups": s.app.CleanupQueue()})
}

func (s *Server) approveCleanup(w http.ResponseWriter, r *http.Request) {
	n, err := s.app.ApproveCleanup(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	s.app.Event("info", "strm", fmt.Sprintf("已确认清理，移除 %d 个残留 strm", n), "")
	writeJSON(w, map[string]any{"ok": true, "removed": n})
}

func (s *Server) cancelCleanup(w http.ResponseWriter, r *http.Request) {
	if err := s.app.CancelCleanup(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}
