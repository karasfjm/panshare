// 企微「企业可信IP」插件的配置层。语义对齐 app/wxip.py 的
// _merged/wxip_settings/save_settings：默认值补齐、非空才覆盖、
// 间隔下限 60 秒。配置存主库 meta（wxip_settings），更新历史存
// wxip_history（保留最近 20 条）。
package core

import (
	"encoding/json"
	"log"
	"strconv"
	"strings"
)

// MetaKey / HistoryMetaKey 是主库 meta 表的两个键名。
const (
	MetaKey        = "wxip_settings"
	HistoryMetaKey = "wxip_history"
)

// Settings 是一份完整配置（对齐 Python DEFAULTS）。
type Settings struct {
	Enabled  bool   `json:"enabled"`   // 总开关
	Auto     bool   `json:"auto"`      // IP 变了自动改（关掉就只提醒）
	AgentIDs string `json:"agent_ids"` // 逗号/换行分隔；留空 = 自动从后台列表抓
	Interval int    `json:"interval"`  // 检测间隔（秒），最小 60
	Notify   bool   `json:"notify"`    // 变化/失败时发通知
	Proxy    string `json:"proxy"`     // 访问企微后台的代理（可选，http://…）
}

// DefaultSettings 对齐 Python DEFAULTS。
func DefaultSettings() Settings {
	return Settings{Enabled: false, Auto: true, Interval: 300, Notify: true}
}

// normalize 兜底：间隔下限 60（0/空按 300），字符串去空白。
func normalize(s Settings) Settings {
	if s.Interval <= 0 {
		s.Interval = 300
	}
	if s.Interval < 60 {
		s.Interval = 60
	}
	s.AgentIDs = strings.TrimSpace(s.AgentIDs)
	s.Proxy = strings.TrimSpace(s.Proxy)
	return s
}

// numOf 把 JSON 数值/字符串数字转 int（前端经常把数字填成字符串）。
func numOf(v any) (int, bool) {
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case int64:
		return int(t), true
	case string:
		if n, err := strconv.Atoi(strings.TrimSpace(t)); err == nil {
			return n, true
		}
		if f, err := strconv.ParseFloat(strings.TrimSpace(t), 64); err == nil {
			return int(f), true
		}
	}
	return 0, false
}

func truthy(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case float64:
		return t != 0
	case string:
		return t == "1" || strings.EqualFold(t, "true") || t == "on"
	}
	return false
}

func strOf(v any) string {
	if s, ok := v.(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}

// Load 读配置（meta），坏 JSON 回默认值。
func Load(st *KV) Settings {
	raw := strings.TrimSpace(st.GetMeta(MetaKey, ""))
	if raw == "" {
		return DefaultSettings()
	}
	var saved Settings
	if err := json.Unmarshal([]byte(raw), &saved); err != nil {
		log.Printf("[wxip] 企微可信IP 配置无法解析，使用默认值: %v", err)
		return DefaultSettings()
	}
	return normalize(saved)
}

// ApplyPatch 把 patch 里「传了且非 null」的字段盖到 cur 上。
func ApplyPatch(cur Settings, patch map[string]any) Settings {
	if v, ok := patch["enabled"]; ok && v != nil {
		cur.Enabled = truthy(v)
	}
	if v, ok := patch["auto"]; ok && v != nil {
		cur.Auto = truthy(v)
	}
	if v, ok := patch["notify"]; ok && v != nil {
		cur.Notify = truthy(v)
	}
	if v, ok := patch["agent_ids"]; ok && v != nil {
		cur.AgentIDs = strOf(v)
	}
	if v, ok := patch["proxy"]; ok && v != nil {
		cur.Proxy = strOf(v)
	}
	if v, ok := patch["interval"]; ok && v != nil {
		if n, ok := numOf(v); ok {
			cur.Interval = n
		}
	}
	return normalize(cur)
}

// Save 读改写一整轮（对齐 Python save_settings）。
func Save(st *KV, patch map[string]any) Settings {
	cur := ApplyPatch(Load(st), patch)
	raw, _ := json.Marshal(cur)
	_ = st.SetMeta(MetaKey, string(raw))
	return cur
}

// AgentList 把配置里的 agent_ids 拆成纯数字 id 列表
// （对齐 Python _agent_list：按逗号/分号/空白拆，只留纯数字段）。
func AgentList(text string) []string {
	var out []string
	for _, x := range strings.FieldsFunc(text, func(r rune) bool {
		return r == ',' || r == '，' || r == ';' || r == '；' ||
			r == ' ' || r == '\t' || r == '\n' || r == '\r'
	}) {
		x = strings.TrimSpace(x)
		if isDigits(x) {
			out = append(out, x)
		}
	}
	return out
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// ------------------------------------------------------------ 更新历史

// History 读更新历史（meta，最多 20 条）。
func History(st *KV) []map[string]any {
	raw := strings.TrimSpace(st.GetMeta(HistoryMetaKey, ""))
	if raw == "" {
		return nil
	}
	var hist []map[string]any
	if err := json.Unmarshal([]byte(raw), &hist); err != nil || hist == nil {
		return nil
	}
	return hist
}

// PushHistory 追加一条并截断到 20 条。
func PushHistory(st *KV, item map[string]any) {
	hist := append(History(st), item)
	if len(hist) > 20 {
		hist = hist[len(hist)-20:]
	}
	raw, _ := json.Marshal(hist)
	_ = st.SetMeta(HistoryMetaKey, string(raw))
}
