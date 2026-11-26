package main

import (
	"encoding/json"
	"testing"
	"time"
)

// 回归：expiresIn 这类大数不能被 firstOf 的字符串化坑掉。
// JSON 里它是 float64，fmt.Sprint 会给出 "1.797691454e+12"，
// 再 Sscan 回来只剩 1 —— 到期时间就被算成「1 秒后」。
func TestFirstIntDoesNotGoThroughString(t *testing.T) {
	m := map[string]any{
		"expiresIn": float64(1797691454000),
		"size":      "12345",
		"count":     json.Number("42"),
	}
	if got := firstInt(m, "expiresIn"); got != 1797691454000 {
		t.Fatalf("expiresIn=%d，期望 1797691454000", got)
	}
	if got := firstInt(m, "size"); got != 12345 {
		t.Fatalf("size=%d，期望 12345", got)
	}
	if got := firstInt(m, "count"); got != 42 {
		t.Fatalf("count=%d，期望 42", got)
	}
	if got := firstInt(m, "nope"); got != 0 {
		t.Fatalf("缺字段应返回 0，得到 %d", got)
	}
}

func TestNormalizeExpiry(t *testing.T) {
	now := nowMS()
	// 毫秒时间戳原样保留
	if got := normalizeExpiry(1797691454000); got != 1797691454000 {
		t.Fatalf("毫秒口径被改了：%d", got)
	}
	// 秒时间戳补成毫秒
	if got := normalizeExpiry(1797691454); got != 1797691454000 {
		t.Fatalf("秒口径转换错误：%d", got)
	}
	// 相对秒数按「现在 + N 秒」
	got := normalizeExpiry(7200)
	if d := got - now; d < 7190*1000 || d > 7210*1000 {
		t.Fatalf("相对秒数算歪了：%d（差 %d ms）", got, d)
	}
	// 0 表示未知
	if got := normalizeExpiry(0); got != 0 {
		t.Fatalf("0 应表示未知，得到 %d", got)
	}
	if time.UnixMilli(1797691454000).Year() != 2026 {
		t.Fatal("样本时间戳不在 2026 年，测试数据要更新")
	}
}
