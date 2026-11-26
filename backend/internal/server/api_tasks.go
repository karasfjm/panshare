package server

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	"panshare/internal/model"
	"panshare/internal/service"
)

// ---------------------------------------------------------------- 分享

func (s *Server) listShares(w http.ResponseWriter, r *http.Request) {
	tasks := s.app.DB.ListShares()
	out := make([]map[string]any, 0, len(tasks))
	for _, t := range tasks {
		name := ""
		if acc, _ := s.app.DB.GetAccount(t.AccountID); acc != nil {
			name = acc.Name
		}
		out = append(out, map[string]any{
			"id": t.ID, "url": t.URL, "title": t.Title, "account_id": t.AccountID,
			"account_name": name, "share_code": t.ShareCode, "receive_code": t.ReceiveCode,
			"dir_cid": t.DirCID, "dir_name": t.DirName, "local_dir": t.LocalDir,
			"no_transfer": t.NoTransfer, "my_share_code": t.MyShareCode,
			"my_receive_code": t.MyReceiveCode, "status": t.Status, "message": t.Message,
			"files": t.Files, "created_at": t.CreatedAt,
		})
	}
	writeJSON(w, map[string]any{"shares": out})
}

func (s *Server) createShare(w http.ResponseWriter, r *http.Request) {
	var req service.ShareRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.requireAccount(req.AccountID); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	task, err := s.app.CreateShare(req)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "task": task})
}

func (s *Server) deleteShare(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	err := s.app.DeleteShare(r.PathValue("id"),
		q.Get("cancel_share") == "1", q.Get("delete_cloud") == "1", q.Get("delete_local") == "1")
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

// ---------------------------------------------------------------- ed2k

func (s *Server) listEd2k(w http.ResponseWriter, r *http.Request) {
	tasks := s.app.DB.ListEd2k()
	out := make([]map[string]any, 0, len(tasks))
	for _, t := range tasks {
		name := ""
		if acc, _ := s.app.DB.GetAccount(t.AccountID); acc != nil {
			name = acc.Name
		}
		out = append(out, map[string]any{
			"id": t.ID, "name": t.Name, "size": t.Size, "url": t.URL,
			"account_id": t.AccountID, "account_name": name, "dir_cid": t.DirCID,
			"local_dir": t.LocalDir, "transfer_on_add": t.TransferOnAdd,
			"state": t.State, "message": t.Message, "pickcode": t.Pickcode,
			"created_at": t.CreatedAt,
		})
	}
	writeJSON(w, map[string]any{"tasks": out})
}

func (s *Server) addEd2k(w http.ResponseWriter, r *http.Request) {
	var req service.Ed2kRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if req.AccountID != "" {
		if err := s.requireAccount(req.AccountID); err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
	}
	tasks, err := s.app.AddEd2k(req)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "tasks": tasks, "count": len(tasks)})
}

func (s *Server) deleteEd2k(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if err := s.app.DeleteEd2k(r.PathValue("id"), q.Get("delete_cloud") == "1", q.Get("delete_local") != "0"); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) exportEd2k(w http.ResponseWriter, r *http.Request) {
	data := s.app.ExportEd2k()
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition",
		fmt.Sprintf(`attachment; filename="panshare-ed2k-%s.json"`, time.Now().Format("20060102")))
	_, _ = w.Write(data)
}

func (s *Server) importEd2k(w http.ResponseWriter, r *http.Request) {
	var data []byte
	ct := r.Header.Get("Content-Type")
	if len(ct) > 19 && ct[:19] == "multipart/form-data" {
		file, _, err := r.FormFile("file")
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		defer file.Close()
		data, _ = io.ReadAll(io.LimitReader(file, 8<<20))
	} else {
		body, _ := io.ReadAll(io.LimitReader(r.Body, 8<<20))
		var payload struct {
			Data      string `json:"data"`
			AccountID string `json:"account_id"`
			DirCID    string `json:"dir_cid"`
			LocalDir  string `json:"local_dir"`
		}
		if err := json.Unmarshal(body, &payload); err == nil && payload.Data != "" {
			data = []byte(payload.Data)
			n, err := s.app.ImportEd2k(data, payload.AccountID, payload.DirCID, payload.LocalDir)
			if err != nil {
				writeErr(w, http.StatusBadRequest, err)
				return
			}
			writeJSON(w, map[string]any{"ok": true, "count": n})
			return
		}
		data = body
	}
	q := r.URL.Query()
	n, err := s.app.ImportEd2k(data, q.Get("account_id"), q.Get("dir_cid"), q.Get("local_dir"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "count": n})
}

// ---------------------------------------------------------------- 秒传

func (s *Server) listInstant(w http.ResponseWriter, r *http.Request) {
	targets := s.app.InstantTargets()
	mappings := s.app.InstantMappings()
	writeJSON(w, map[string]any{"targets": targets, "mappings": mappings})
}

func (s *Server) saveInstantTarget(w http.ResponseWriter, r *http.Request) {
	var t model.InstantTarget
	if err := json.NewDecoder(r.Body).Decode(&t); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.requireAccount(t.AccountID); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.app.SaveInstantTarget(t); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) deleteInstantTarget(w http.ResponseWriter, r *http.Request) {
	if err := s.app.DeleteInstantTarget(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) saveInstantMapping(w http.ResponseWriter, r *http.Request) {
	var m model.InstantMapping
	if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.app.SaveInstantMapping(m); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) deleteInstantMapping(w http.ResponseWriter, r *http.Request) {
	if err := s.app.DeleteInstantMapping(r.PathValue("id")); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

var _ = strconv.Itoa

// ---------------------------------------------------------------- 页面记忆项

// taskDefaults 返回分享 / ed2k 页面「上次用的账号、接收目录、本地目录」。
// 这三项用户要求固定住，换页、重启浏览器都不用重选。
func (s *Server) taskDefaults(w http.ResponseWriter, r *http.Request) {
	st := s.app.Settings()
	writeJSON(w, map[string]any{
		"share": map[string]any{
			"account_id": st.ShareAccountID, "dir_cid": st.ShareDirCID,
			"dir_name": st.ShareDirName, "local_dir": st.ShareLocalDir,
		},
		"ed2k": map[string]any{
			"account_id": st.Ed2kAccountID, "dir_cid": st.Ed2kDirCID,
			"dir_name": st.Ed2kDirName, "local_dir": st.Ed2kLocalDir,
		},
	})
}

// saveTaskDefaults 记住某一页当前选中的账号 / 目录。
func (s *Server) saveTaskDefaults(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Kind      string `json:"kind"`
		AccountID string `json:"account_id"`
		DirCID    string `json:"dir_cid"`
		DirName   string `json:"dir_name"`
		LocalDir  string `json:"local_dir"`
	}
	if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	st := s.app.Settings()
	switch b.Kind {
	case "share":
		st.ShareAccountID, st.ShareDirCID = b.AccountID, b.DirCID
		st.ShareDirName, st.ShareLocalDir = b.DirName, b.LocalDir
	case "ed2k":
		st.Ed2kAccountID, st.Ed2kDirCID = b.AccountID, b.DirCID
		st.Ed2kDirName, st.Ed2kLocalDir = b.DirName, b.LocalDir
	default:
		writeErr(w, http.StatusBadRequest, fmt.Errorf("kind 只能是 share 或 ed2k"))
		return
	}
	if err := s.app.SaveSettings(st); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}
