package service

import (
	"testing"

	"panshare/internal/model"
	"panshare/internal/strm"
)

func TestCleanRemote(t *testing.T) {
	cases := map[string]string{
		"":            "",
		"/":           "",
		"/LTX/已整理/":   "/LTX/已整理",
		"LTX//已整理":    "/LTX/已整理",
		" LTX/已整理 ":   "/LTX/已整理",
		`\LTX\VR-已整理`: "/LTX/VR-已整理",
	}
	for in, want := range cases {
		if got := cleanRemote(in); got != want {
			t.Fatalf("cleanRemote(%q) = %q, want %q", in, got, want)
		}
	}
}

// 事件只给 parent_id，拼出网盘路径后要能判断落不落在这条配置的目录下。
func TestMatchRemotePath(t *testing.T) {
	a := &App{Store: &strm.Store{Root: "/strm/panshare/115"}}
	c := &model.StrmConfig{
		Paths: []model.StrmPath{
			{RemoteID: "1", RemotePath: "/LTX/已整理", LocalDir: "LTX/已整理"},
			{RemoteID: "2", RemotePath: "/LTX/VR-已整理", LocalDir: "/strm/panshare/115/LTX/VR"},
		},
	}

	m, ok := a.matchRemotePath(c, "/LTX/已整理/堕落物语2")
	if !ok {
		t.Fatal("应该命中第一条映射")
	}
	if m.localPrefix != "LTX/已整理" || m.relDir != "堕落物语2" {
		t.Fatalf("命中结果不对: %+v", m)
	}

	m, ok = a.matchRemotePath(c, "/LTX/VR-已整理/电影")
	if !ok || m.localPrefix != "LTX/VR" || m.relDir != "电影" {
		t.Fatalf("第二条映射命中不对: %+v ok=%v", m, ok)
	}

	if _, ok := a.matchRemotePath(c, "/LTX/别的目录"); ok {
		t.Fatal("不在监控目录里的不该命中（否则会把无关文件也写成 strm）")
	}
	if _, ok := a.matchRemotePath(c, "/LTX/已整理2"); ok {
		t.Fatal("前缀相同但不是子目录的不该命中")
	}
}

// 没有配网盘路径（根目录）时，任何路径都算命中。
func TestMatchRemotePathRoot(t *testing.T) {
	a := &App{Store: &strm.Store{Root: "/strm"}}
	c := &model.StrmConfig{Paths: []model.StrmPath{{RemoteID: "0", RemotePath: "/", LocalDir: "all"}}}
	m, ok := a.matchRemotePath(c, "/随意/子目录")
	if !ok || m.relDir != "随意/子目录" {
		t.Fatalf("根目录映射命中不对: %+v ok=%v", m, ok)
	}
}

// 老配置只有 remote_id/local_dir 三个字段，EffectivePaths 要能回落。
func TestEffectivePathsFallback(t *testing.T) {
	c := &model.StrmConfig{RemoteID: "", RemotePath: "/国漫", LocalDir: "国漫"}
	paths := c.EffectivePaths()
	if len(paths) != 1 || paths[0].RemoteID != "0" || paths[0].LocalDir != "国漫" {
		t.Fatalf("回落结果不对: %+v", paths)
	}
}
