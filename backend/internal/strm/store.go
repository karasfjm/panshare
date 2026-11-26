// Package strm 负责本地 strm 文件的写入、扫描与联动清理。
//
// 一条铁律：所有路径都必须落在映射根（STRM_ROOT）下面。用户填的本地目录
// 可能是 /strm/国漫、strm/国漫、国漫 三种写法，统一在这里归一化。
package strm

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

type Store struct {
	Root string
}

func New(root string) (*Store, error) {
	if strings.TrimSpace(root) == "" {
		return nil, fmt.Errorf("映射根不能为空")
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(abs, 0o755); err != nil {
		return nil, err
	}
	return &Store{Root: abs}, nil
}

// CleanRel 把用户填的目录/文件名归一化成相对映射根的安全路径。
func (s *Store) CleanRel(rel string) string {
	r := strings.ReplaceAll(rel, "\\", "/")
	r = strings.TrimSpace(r)
	rootName := filepath.Base(s.Root)
	// 先按「整条映射根的绝对路径」剥一层：/strm/panshare/115/LTX → LTX。
	// 页面上展示的是绝对路径，直接粘回来也得认。
	rootSlash := filepath.ToSlash(s.Root)
	if r == rootSlash {
		return ""
	}
	if strings.HasPrefix(r, rootSlash+"/") {
		r = strings.TrimPrefix(r, rootSlash+"/")
	}
	r = strings.TrimPrefix(r, "/")
	r = strings.TrimPrefix(r, rootName+"/")
	if r == rootName {
		return ""
	}
	r = strings.TrimPrefix(r, "./")
	// 绝对路径但不在映射根下：只取最后一段之后的相对部分由调用方决定，
	// 这里保留剩下的层级（Clean 后仍会做一次前缀校验）
	parts := make([]string, 0, 8)
	for _, p := range strings.Split(r, "/") {
		if p == "" || p == "." {
			continue
		}
		if p == ".." {
			continue
		}
		parts = append(parts, p)
	}
	return strings.Join(parts, "/")
}

// Abs 返回绝对路径，并校验没有越出映射根。
func (s *Store) Abs(rel string) (string, error) {
	clean := s.CleanRel(rel)
	abs := filepath.Join(s.Root, filepath.FromSlash(clean))
	abs = filepath.Clean(abs)
	if abs != s.Root && !strings.HasPrefix(abs, s.Root+string(os.PathSeparator)) {
		return "", fmt.Errorf("路径越界: %s", rel)
	}
	return abs, nil
}

// Write 原子写入一个 strm（内容相同则跳过，返回 changed=false）。
func (s *Store) Write(rel, content string) (string, bool, error) {
	abs, err := s.Abs(rel)
	if err != nil {
		return "", false, err
	}
	if !strings.HasSuffix(strings.ToLower(abs), ".strm") {
		abs += ".strm"
	}
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return "", false, err
	}
	if old, err := os.ReadFile(abs); err == nil && string(old) == content {
		return abs, false, nil
	}
	tmp := abs + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0o644); err != nil {
		return "", false, err
	}
	if err := os.Rename(tmp, abs); err != nil {
		_ = os.Remove(tmp)
		return "", false, err
	}
	return abs, true, nil
}

// Read 读 strm 内容（只读第一行）。
func (s *Store) Read(abs string) string {
	f, err := os.Open(abs)
	if err != nil {
		return ""
	}
	defer f.Close()
	buf := make([]byte, 4096)
	n, _ := f.Read(buf)
	return strings.TrimSpace(string(buf[:n]))
}

var metaExts = map[string]bool{
	".nfo": true, ".srt": true, ".ass": true, ".ssa": true, ".vtt": true,
	".sub": true, ".idx": true, ".sup": true, ".jpg": true, ".jpeg": true,
	".png": true, ".webp": true, ".bmp": true, ".gif": true, ".txt": true,
}

// MetaFiles 返回与某个 strm 同名的附属文件（nfo / 字幕 / 海报）。
// 规则：同目录下、以文件名（不含扩展名）开头、后缀属于元数据类型的文件。
func MetaFiles(strmPath string) []string {
	dir := filepath.Dir(strmPath)
	base := strings.TrimSuffix(filepath.Base(strmPath), filepath.Ext(strmPath))
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	out := make([]string, 0, 8)
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if !strings.HasPrefix(name, base) {
			continue
		}
		ext := strings.ToLower(filepath.Ext(name))
		if !metaExts[ext] {
			continue
		}
		// 只接受「同名.后缀」或「同名-xxx.后缀」，避免误删同名剧集的其它文件
		rest := strings.TrimPrefix(name, base)
		if rest != "" && !strings.HasPrefix(rest, ".") && !strings.HasPrefix(rest, "-") {
			continue
		}
		out = append(out, filepath.Join(dir, name))
	}
	return out
}

// Remove 删除一个 strm（相对路径）。
func (s *Store) Remove(rel string) bool {
	abs, err := s.Abs(rel)
	if err != nil {
		return false
	}
	if !strings.HasSuffix(strings.ToLower(abs), ".strm") {
		abs += ".strm"
	}
	if err := os.Remove(abs); err != nil {
		return false
	}
	s.pruneEmpty(filepath.Dir(abs))
	return true
}

// RemoveAbs 删除绝对路径的 strm，并返回一并删掉的附属文件。
func (s *Store) RemoveAbs(abs string, withMeta bool) ([]string, error) {
	if !strings.HasPrefix(filepath.Clean(abs), s.Root) {
		return nil, fmt.Errorf("拒绝删除映射根之外的路径: %s", abs)
	}
	var removed []string
	if withMeta {
		for _, m := range MetaFiles(abs) {
			if err := os.Remove(m); err == nil {
				removed = append(removed, m)
			}
		}
	}
	if err := os.Remove(abs); err != nil && !os.IsNotExist(err) {
		return removed, err
	}
	s.pruneEmpty(filepath.Dir(abs))
	return removed, nil
}

// pruneEmpty 删除空目录，但不会删掉映射根本身。
func (s *Store) pruneEmpty(dir string) {
	for dir != s.Root && strings.HasPrefix(dir, s.Root) {
		entries, err := os.ReadDir(dir)
		if err != nil || len(entries) > 0 {
			return
		}
		if err := os.Remove(dir); err != nil {
			return
		}
		dir = filepath.Dir(dir)
	}
}

// Scan 扫描映射根下所有 strm，返回相对路径集合（用 / 分隔）。
func (s *Store) Scan() (map[string]bool, error) {
	out := map[string]bool{}
	err := filepath.WalkDir(s.Root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}
		if !strings.HasSuffix(strings.ToLower(d.Name()), ".strm") {
			return nil
		}
		rel, err := filepath.Rel(s.Root, path)
		if err == nil {
			out[filepath.ToSlash(rel)] = true
		}
		return nil
	})
	return out, err
}

// Sanitize 把网盘里的名字变成安全的文件名。
func Sanitize(name string, maxBytes int) string {
	if maxBytes <= 0 {
		maxBytes = 180
	}
	replacer := strings.NewReplacer(
		"/", "／", "\\", "＼", ":", "：", "*", "＊", "?", "？",
		"\"", "＂", "<", "＜", ">", "＞", "|", "｜", "\x00", "",
	)
	out := replacer.Replace(strings.TrimSpace(name))
	out = strings.TrimRight(out, ". ")
	if out == "" {
		out = "未命名"
	}
	if len(out) > maxBytes {
		// 按 rune 截断，别把 UTF-8 截坏
		var b strings.Builder
		for _, r := range out {
			if b.Len()+utf8.RuneLen(r) > maxBytes {
				break
			}
			b.WriteRune(r)
		}
		out = b.String()
	}
	return out
}
