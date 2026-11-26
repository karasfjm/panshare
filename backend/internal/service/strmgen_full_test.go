package service

import (
	"testing"
	"time"

	"panshare/internal/model"
)

func TestParseCron(t *testing.T) {
	cases := []struct {
		expr string
		when time.Time
		want bool
	}{
		{"0 3 * * *", time.Date(2026, 11, 26, 3, 0, 0, 0, time.UTC), true},
		{"0 3 * * *", time.Date(2026, 11, 26, 4, 0, 0, 0, time.UTC), false},
		{"*/15 * * * *", time.Date(2026, 11, 26, 10, 45, 0, 0, time.UTC), true},
		{"30 2 * * 1-5", time.Date(2026, 11, 26, 2, 30, 0, 0, time.UTC), true}, // 2026-11-26 是周四
		{"0 0 1 * *", time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC), true},
	}
	for _, tc := range cases {
		e, err := parseCron(tc.expr)
		if err != nil {
			t.Fatalf("parse %q: %v", tc.expr, err)
		}
		if got := e.match(tc.when); got != tc.want {
			t.Errorf("%q @ %s = %v, want %v", tc.expr, tc.when, got, tc.want)
		}
	}
	if _, err := parseCron("0 3 * *"); err == nil {
		t.Fatal("字段不足应报错")
	}
	if _, err := parseCron("99 3 * * *"); err == nil {
		t.Fatal("越界应报错")
	}
}

func TestStrmFilterAllow(t *testing.T) {
	f := strmFilter{
		allFiles:    false,
		keywords:    []string{"花絮", "sample"},
		minBytes:    100 << 20,
		skipPending: true,
		pendingDirs: []string{"待整理"},
	}
	f.exts = map[string]bool{".mkv": true}
	cases := []struct {
		name string
		size int64
		want bool
	}{
		{"movie.mkv", 200 << 20, true},
		{"movie.mp4", 200 << 20, false},
		{"movie-花絮.mkv", 200 << 20, false},
		{"sample.mkv", 200 << 20, false},
		{"tiny.mkv", 10 << 20, false},
	}
	for _, tc := range cases {
		if got := f.allow(tc.name, tc.size); got != tc.want {
			t.Errorf("allow(%q) = %v, want %v", tc.name, got, tc.want)
		}
	}
	if !f.skipDir("待整理2025") || f.skipDir("电影") {
		t.Fatal("待整理目录判断不对")
	}
	if got := splitList("花絮，sample\n预告"); len(got) != 3 {
		t.Fatalf("splitList = %v", got)
	}
}

func TestStaleSignature(t *testing.T) {
	a := []model.StrmFile{{FileID: "1"}, {FileID: "2"}, {FileID: "3"}}
	b := []model.StrmFile{{FileID: "3"}, {FileID: "1"}, {FileID: "2"}}
	if staleSignature(a) != staleSignature(b) {
		t.Fatal("同一批残留、不同顺序应得到相同指纹")
	}
	if staleSignature(a) == staleSignature(a[:2]) {
		t.Fatal("残留集合变化后指纹应变化")
	}
	if staleSignature(nil) != "" {
		t.Fatal("空集合指纹应为空")
	}
}
