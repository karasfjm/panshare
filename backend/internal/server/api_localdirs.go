package server

import (
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// localDirs 给「本地 STRM 目录」的【选】按钮用：只允许在映射根下面浏览。
//
// 页面上填的本地目录一律是「相对映射根」的形式（/strm/panshare/115/LTX
// 这种绝对写法会被 CleanRel 折成 115/LTX），所以这里也只在映射根里逛，
// 既安全，也避免用户填出一个 strm 落在映射根外面的目录。
func (s *Server) localDirs(w http.ResponseWriter, r *http.Request) {
	root := s.app.Store.Root
	raw := strings.TrimSpace(r.URL.Query().Get("path"))
	rel := s.app.Store.CleanRel(raw)
	abs, err := s.app.Store.Abs(rel)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	entries, err := os.ReadDir(abs)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	dirs := make([]map[string]any, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		dirs = append(dirs, map[string]any{
			"name": e.Name(),
			"rel":  filepath.ToSlash(filepath.Join(rel, e.Name())),
			"abs":  filepath.Join(abs, e.Name()),
		})
	}
	sort.Slice(dirs, func(i, j int) bool {
		return dirs[i]["name"].(string) < dirs[j]["name"].(string)
	})
	parent := ""
	if rel != "" {
		parent = filepath.ToSlash(filepath.Dir(rel))
		if parent == "." {
			parent = ""
		}
	}
	writeJSON(w, map[string]any{
		"root":   root,
		"rel":    rel,
		"abs":    abs,
		"parent": parent,
		"dirs":   dirs,
	})
}
