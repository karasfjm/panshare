package server

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"panshare/internal/model"
)

func (s *Server) accounts(w http.ResponseWriter, r *http.Request) {
	accs := s.app.DB.ListAccounts()
	out := make([]map[string]any, 0, len(accs))
	for _, a := range accs {
		cfgs := 0
		for _, c := range s.app.DB.ListConfigs() {
			if c.AccountID == a.ID {
				cfgs++
			}
		}
		out = append(out, map[string]any{
			"id": a.ID, "name": a.Name, "uid": a.UID, "user_name": a.UserName,
			"source": a.Source, "has_cookies": a.HasCookies, "copy_play": a.CopyPlay,
			"is_default": a.IsDefault, "status": a.Status, "last_error": a.LastError,
			"configs": cfgs, "created_at": a.CreatedAt,
		})
	}
	writeJSON(w, map[string]any{"accounts": out, "default": defaultID(s)})
}

func defaultID(s *Server) string {
	if acc := s.app.DB.DefaultAccount(); acc != nil {
		return acc.ID
	}
	return ""
}

func (s *Server) addCookie(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Cookies string `json:"cookies"`
		Name    string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	acc, err := s.app.AddAccountByCookie(ctx, body.Cookies, body.Name)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "account": acc})
}

func (s *Server) startQR(w http.ResponseWriter, r *http.Request) {
	var body struct {
		AccountID string `json:"account_id"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	res, err := s.app.StartQR(ctx, body.AccountID)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, res)
}

func (s *Server) pollQR(w http.ResponseWriter, r *http.Request) {
	uid := r.PathValue("uid")
	name := r.URL.Query().Get("name")
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	res, err := s.app.PollQR(ctx, uid, name)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, res)
}

func (s *Server) updateAccount(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.app.UpdateAccount(id, body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	acc, _ := s.app.DB.GetAccount(id)
	writeJSON(w, map[string]any{"ok": true, "account": acc})
}

func (s *Server) deleteAccount(w http.ResponseWriter, r *http.Request) {
	n, err := s.app.DeleteAccount(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "removed_configs": n})
}

func (s *Server) refreshAccount(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	acc, err := s.app.RefreshAccount(ctx, r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "account": acc})
}

func (s *Server) setDefaultAccount(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := s.app.DB.GetAccount(id); err != nil {
		writeErr(w, http.StatusNotFound, err)
		return
	}
	if err := s.app.DB.SetDefaultAccount(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) dirs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	res, err := s.app.ListDirs(ctx, r.URL.Query().Get("account_id"), r.URL.Query().Get("cid"))
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, res)
}

var _ = model.Account{}
