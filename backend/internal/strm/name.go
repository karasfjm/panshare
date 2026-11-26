package strm

import (
	"path"
	"strings"
)

// Ext 返回小写扩展名（含点）。
func Ext(name string) string {
	return strings.ToLower(path.Ext(strings.TrimSpace(name)))
}

// Stem 返回去掉扩展名的文件名。
func Stem(name string) string {
	base := path.Base(name)
	ext := path.Ext(base)
	return strings.TrimSuffix(base, ext)
}

// ParseExts 把 "mp4,mkv,.ts" 解析成集合。
func ParseExts(raw string) map[string]bool {
	out := map[string]bool{}
	for _, part := range strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == ';' || r == '，' || r == ' ' || r == '\n' || r == '\t'
	}) {
		p := strings.ToLower(strings.TrimSpace(part))
		if p == "" {
			continue
		}
		if !strings.HasPrefix(p, ".") {
			p = "." + p
		}
		out[p] = true
	}
	return out
}

// MatchExt 判断文件是否属于要生成 strm 的类型；all=true 时全部匹配。
func MatchExt(name string, exts map[string]bool, all bool) bool {
	if all || len(exts) == 0 {
		return true
	}
	return exts[Ext(name)]
}

// IsMeta 判断是否是元数据文件（nfo / 字幕 / 图片）。
func IsMeta(name string) bool {
	switch Ext(name) {
	case ".nfo", ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup",
		".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".txt":
		return true
	}
	return false
}
