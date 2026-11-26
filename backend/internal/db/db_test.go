package db

import (
	"database/sql"
	"path/filepath"
	"testing"
)

// 老版本建的库没有 watch_interval / last_sig 两列。
// 升级后必须能自动补上，否则老用户一升级就是 "no such column"。
func TestMigrateAddsColumnsToOldDB(t *testing.T) {
	path := filepath.Join(t.TempDir(), "old.db")

	// 1. 用旧结构建库并塞一条配置
	conn, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("打开旧库失败: %v", err)
	}
	_, err = conn.Exec(`CREATE TABLE strm_configs (
		id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
		account_id TEXT NOT NULL DEFAULT '', remote_id TEXT NOT NULL DEFAULT '0',
		remote_path TEXT NOT NULL DEFAULT '', local_dir TEXT NOT NULL DEFAULT '',
		mode TEXT NOT NULL DEFAULT 'pickcode', include_exts TEXT NOT NULL DEFAULT '',
		all_files INTEGER NOT NULL DEFAULT 0, watch_enabled INTEGER NOT NULL DEFAULT 0,
		watch_delay INTEGER NOT NULL DEFAULT 0, sched_enabled INTEGER NOT NULL DEFAULT 0,
		sched_minutes INTEGER NOT NULL DEFAULT 0, download_meta INTEGER NOT NULL DEFAULT 0,
		last_run_at INTEGER NOT NULL DEFAULT 0, last_status TEXT NOT NULL DEFAULT '',
		last_message TEXT NOT NULL DEFAULT '', files INTEGER NOT NULL DEFAULT 0,
		created_at INTEGER NOT NULL DEFAULT 0);`)
	if err != nil {
		t.Fatalf("建旧表失败: %v", err)
	}
	if _, err = conn.Exec(`INSERT INTO strm_configs (id, name, watch_enabled) VALUES ('c1','国漫',1)`); err != nil {
		t.Fatalf("写旧数据失败: %v", err)
	}
	conn.Close()

	// 2. 用新版本打开 → 应自动补列
	d, err := Open(path)
	if err != nil {
		t.Fatalf("升级失败: %v", err)
	}
	defer d.Close()

	cfgs := d.ListConfigs()
	if len(cfgs) != 1 {
		t.Fatalf("期望读到 1 条配置，实际 %d", len(cfgs))
	}
	if cfgs[0].Name != "国漫" || !cfgs[0].WatchEnabled {
		t.Fatalf("老数据读错了: %+v", cfgs[0])
	}
	if cfgs[0].WatchInterval != 0 || cfgs[0].LastSig != "" {
		t.Fatalf("新列应有零值，实际 interval=%d sig=%q", cfgs[0].WatchInterval, cfgs[0].LastSig)
	}

	// 3. 新列能正常写入与读回
	if err := d.UpdateConfig("c1", map[string]any{"watch_interval": 30, "last_sig": "abc123"}); err != nil {
		t.Fatalf("写新列失败: %v", err)
	}
	got, err := d.GetConfig("c1")
	if err != nil || got == nil {
		t.Fatalf("读配置失败: %v", err)
	}
	if got.WatchInterval != 30 || got.LastSig != "abc123" {
		t.Fatalf("新列读回不对: interval=%d sig=%q", got.WatchInterval, got.LastSig)
	}

	// 4. 再 Open 一次要幂等（列已存在不能报错）
	d.Close()
	d2, err := Open(path)
	if err != nil {
		t.Fatalf("二次升级（幂等）失败: %v", err)
	}
	d2.Close()
}

// 老库里 accounts.uid 存的是整条 UID cookie（2806858_R1_1794486118），
// 打开时必须剪成纯数字，否则 strm 末尾会带一长串，换客户端登录后就失配。
func TestNormalizeUIDOnOpen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "acc.db")
	d, err := Open(path)
	if err != nil {
		t.Fatalf("打开失败: %v", err)
	}
	if _, err := d.sql.Exec(`INSERT INTO accounts (id, name, uid, cookies, created_at)
		VALUES ('a1','16T','2806858_R1_1794486118','UID=2806858_R1_1794486118; CID=x',1),
		       ('a2','香菜','331466418','UID=331466418; CID=y',2)`); err != nil {
		t.Fatalf("写入失败: %v", err)
	}
	d.Close()

	d2, err := Open(path)
	if err != nil {
		t.Fatalf("重开失败: %v", err)
	}
	defer d2.Close()
	got := map[string]string{}
	for _, a := range d2.ListAccounts() {
		got[a.ID] = a.UID
	}
	if got["a1"] != "2806858" {
		t.Errorf("复合 UID 应剪成 2806858，得到 %q", got["a1"])
	}
	if got["a2"] != "331466418" {
		t.Errorf("纯数字 UID 不应被改，得到 %q", got["a2"])
	}
	if acc, _ := d2.GetAccountByUID("2806858"); acc == nil {
		t.Error("按纯数字 uid 应该能查到账号")
	}
}
