// Package db 是 SQLite 持久层：账号、生成配置、索引、任务、设置与事件都在这里。
//
// 之所以用 SQLite 而不是 JSON：STRM 索引动辄十万行，全量重扫时还要按 file_id
// 做增量比对，JSON 每次都要整份读写，既慢又容易在掉电时写坏。
package db

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"panshare/internal/model"
	"panshare/internal/p115"
)

type DB struct {
	sql *sql.DB
}

func Open(path string) (*DB, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	dsn := path + "?_pragma=busy_timeout(10000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)"
	conn, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// SQLite 单写多读：连接数控制住，避免 database is locked
	conn.SetMaxOpenConns(8)
	conn.SetMaxIdleConns(4)
	if err := conn.Ping(); err != nil {
		return nil, err
	}
	d := &DB{sql: conn}
	if err := d.migrate(); err != nil {
		return nil, err
	}
	return d, nil
}

func (d *DB) Close() error { return d.sql.Close() }

const schema = `
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS accounts (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL DEFAULT '',
  uid         TEXT NOT NULL DEFAULT '',
  user_name   TEXT NOT NULL DEFAULT '',
  cookies     TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT 'cookie',
  copy_play   INTEGER NOT NULL DEFAULT 0,
  is_default  INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'ok',
  last_error  TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_accounts_uid ON accounts(uid);

CREATE TABLE IF NOT EXISTS strm_configs (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL DEFAULT '',
  account_id     TEXT NOT NULL DEFAULT '',
  remote_id      TEXT NOT NULL DEFAULT '0',
  remote_path    TEXT NOT NULL DEFAULT '',
  local_dir      TEXT NOT NULL DEFAULT '',
  paths          TEXT NOT NULL DEFAULT '',
  strm_base      TEXT NOT NULL DEFAULT '',
  meta_exts      TEXT NOT NULL DEFAULT '',
  mode           TEXT NOT NULL DEFAULT 'pickcode',
  include_exts   TEXT NOT NULL DEFAULT '',
  all_files      INTEGER NOT NULL DEFAULT 0,
  watch_enabled  INTEGER NOT NULL DEFAULT 0,
  watch_delay    INTEGER NOT NULL DEFAULT 0,
  watch_interval INTEGER NOT NULL DEFAULT 0,
  last_sig       TEXT NOT NULL DEFAULT '',
  last_check_at  INTEGER NOT NULL DEFAULT 0,
  sched_enabled  INTEGER NOT NULL DEFAULT 0,
  sched_minutes  INTEGER NOT NULL DEFAULT 0,
  download_meta  INTEGER NOT NULL DEFAULT 0,
  last_run_at    INTEGER NOT NULL DEFAULT 0,
  last_status    TEXT NOT NULL DEFAULT '',
  last_message   TEXT NOT NULL DEFAULT '',
  files          INTEGER NOT NULL DEFAULT 0,
  created_at     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strm_files (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id  TEXT NOT NULL DEFAULT '',
  account_id TEXT NOT NULL DEFAULT '',
  file_id    TEXT NOT NULL DEFAULT '',
  pickcode   TEXT NOT NULL DEFAULT '',
  name       TEXT NOT NULL DEFAULT '',
  rel_path   TEXT NOT NULL DEFAULT '',
  strm_path  TEXT NOT NULL DEFAULT '',
  size       INTEGER NOT NULL DEFAULT 0,
  sha1       TEXT NOT NULL DEFAULT '',
  parent_id  TEXT NOT NULL DEFAULT '',
  share_code TEXT NOT NULL DEFAULT '',
  source     TEXT NOT NULL DEFAULT 'strm',
  deleted    INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  UNIQUE(config_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_files_config ON strm_files(config_id);
CREATE INDEX IF NOT EXISTS idx_files_pickcode ON strm_files(pickcode);
CREATE INDEX IF NOT EXISTS idx_files_strm ON strm_files(strm_path);
CREATE INDEX IF NOT EXISTS idx_files_sha1 ON strm_files(sha1);

-- 全量清理的待确认队列：生成端只写计划，人工确认后才真正删文件。
CREATE TABLE IF NOT EXISTS cleanup_confirmations (
  id          TEXT PRIMARY KEY,
  config_id   TEXT NOT NULL DEFAULT '',
  config_name TEXT NOT NULL DEFAULT '',
  stale_sig   TEXT NOT NULL DEFAULT '',
  items       TEXT NOT NULL DEFAULT '[]',
  total       INTEGER NOT NULL DEFAULT 0,
  ratio       INTEGER NOT NULL DEFAULT 0,
  state       TEXT NOT NULL DEFAULT 'pending',
  created_at  INTEGER NOT NULL DEFAULT 0,
  expires_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cleanup_state ON cleanup_confirmations(state);

CREATE TABLE IF NOT EXISTS share_tasks (
  id             TEXT PRIMARY KEY,
  account_id     TEXT NOT NULL DEFAULT '',
  url            TEXT NOT NULL DEFAULT '',
  share_code     TEXT NOT NULL DEFAULT '',
  receive_code   TEXT NOT NULL DEFAULT '',
  title          TEXT NOT NULL DEFAULT '',
  dir_cid        TEXT NOT NULL DEFAULT '',
  dir_name       TEXT NOT NULL DEFAULT '',
  local_dir      TEXT NOT NULL DEFAULT '',
  no_transfer    INTEGER NOT NULL DEFAULT 0,
  my_share_code  TEXT NOT NULL DEFAULT '',
  my_receive_code TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'pending',
  message        TEXT NOT NULL DEFAULT '',
  files          INTEGER NOT NULL DEFAULT 0,
  created_at     INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ed2k_tasks (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL DEFAULT '',
  size           INTEGER NOT NULL DEFAULT 0,
  url            TEXT NOT NULL DEFAULT '',
  account_id     TEXT NOT NULL DEFAULT '',
  dir_cid        TEXT NOT NULL DEFAULT '',
  local_dir      TEXT NOT NULL DEFAULT '',
  transfer_on_add INTEGER NOT NULL DEFAULT 0,
  state          TEXT NOT NULL DEFAULT 'pending',
  message        TEXT NOT NULL DEFAULT '',
  pickcode       TEXT NOT NULL DEFAULT '',
  file_id        TEXT NOT NULL DEFAULT '',
  created_at     INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS instant_targets (
  account_id TEXT PRIMARY KEY,
  dir_cid    TEXT NOT NULL DEFAULT '0',
  dir_path   TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS instant_map (
  emby_user_id   TEXT PRIMARY KEY,
  emby_user_name TEXT NOT NULL DEFAULT '',
  account_id     TEXT NOT NULL DEFAULT '',
  updated_at     INTEGER NOT NULL DEFAULT 0
);

-- 秒传二次校验的数据块：115 要的那段字节只跟文件内容有关，
-- 所以按 (sha1, 块起点) 存下来，同一个人重播、或换个人播同一部片子时
-- 不用再回源盘读一次。纯加速用，丢了最多多读一次源盘。
CREATE TABLE IF NOT EXISTS instant_digest (
  sha1    TEXT NOT NULL,
  off     INTEGER NOT NULL,
  size    INTEGER NOT NULL DEFAULT 0,
  data    BLOB,
  used_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (sha1, off)
);

CREATE TABLE IF NOT EXISTS emby_servers (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL DEFAULT '',
  url        TEXT NOT NULL DEFAULT '',
  api_key    TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  level      TEXT NOT NULL DEFAULT 'info',
  kind       TEXT NOT NULL DEFAULT '',
  message    TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS deletions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL DEFAULT '',
  account_id TEXT NOT NULL DEFAULT '',
  file_id    TEXT NOT NULL DEFAULT '',
  path       TEXT NOT NULL DEFAULT '',
  strm_path  TEXT NOT NULL DEFAULT '',
  state      TEXT NOT NULL DEFAULT 'pending',
  message    TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0
);
`

func (d *DB) migrate() error {
	if _, err := d.sql.Exec(schema); err != nil {
		return err
	}
	// CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，老库升级时在这里补。
	// 全部是幂等的：列已存在就跳过。
	for _, m := range []struct{ table, col, ddl string }{
		{"strm_configs", "watch_interval", "INTEGER NOT NULL DEFAULT 0"},
		{"strm_configs", "last_sig", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "last_check_at", "INTEGER NOT NULL DEFAULT 0"},
		{"strm_configs", "paths", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "strm_base", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "meta_exts", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "overwrite_mode", "TEXT NOT NULL DEFAULT 'always'"},
		{"strm_configs", "exclude_keywords", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "min_file_size_mb", "INTEGER NOT NULL DEFAULT 0"},
		{"strm_configs", "skip_pending", "INTEGER NOT NULL DEFAULT 0"},
		{"strm_configs", "pending_dirs", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "concurrency", "INTEGER NOT NULL DEFAULT 0"},
		{"strm_configs", "cron_full", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "cleanup_stale_sig", "TEXT NOT NULL DEFAULT ''"},
		{"strm_configs", "cleanup_stale_count", "INTEGER NOT NULL DEFAULT 0"},
	} {
		has, err := d.hasColumn(m.table, m.col)
		if err != nil {
			return err
		}
		if has {
			continue
		}
		if _, err := d.sql.Exec("ALTER TABLE " + m.table + " ADD COLUMN " + m.col + " " + m.ddl); err != nil {
			return fmt.Errorf("升级表 %s 增加列 %s 失败: %w", m.table, m.col, err)
		}
	}
	return d.normalizeUIDs()
}

// normalizeUIDs 把老库里存成整条 UID cookie 的账号剪成纯数字。
//
// 老版本直接存了 `2806858_R1_1794486118` 这种复合串，写进 strm 的地址末尾
// 就是那一长串；换客户端登录后 UID 会变，旧 strm 立刻对不上账号。
// 这里幂等：uid 已经是纯数字就一条都不改。
func (d *DB) normalizeUIDs() error {
	rows, err := d.sql.Query(`SELECT id, uid FROM accounts WHERE uid <> ''`)
	if err != nil {
		return err
	}
	type pair struct{ id, uid string }
	var fix []pair
	for rows.Next() {
		var p pair
		if err := rows.Scan(&p.id, &p.uid); err != nil {
			rows.Close()
			return err
		}
		if clean := p115.CleanUID(p.uid); clean != "" && clean != p.uid {
			fix = append(fix, pair{p.id, clean})
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, p := range fix {
		if _, err := d.sql.Exec(`UPDATE accounts SET uid = ? WHERE id = ? AND uid <> ?`,
			p.uid, p.id, p.uid); err != nil {
			return err
		}
	}
	return nil
}

func (d *DB) hasColumn(table, col string) (bool, error) {
	rows, err := d.sql.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var (
			cid     int
			name    string
			ctype   string
			notnull int
			dflt    any
			pk      int
		)
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			return false, err
		}
		if name == col {
			return true, nil
		}
	}
	return false, rows.Err()
}

func now() int64 { return time.Now().Unix() }

func (d *DB) update(table, idCol, id string, fields map[string]any) error {
	if len(fields) == 0 {
		return nil
	}
	keys := make([]string, 0, len(fields))
	for k := range fields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	sets := make([]string, 0, len(keys))
	args := make([]any, 0, len(keys)+1)
	for _, k := range keys {
		sets = append(sets, k+" = ?")
		args = append(args, fields[k])
	}
	args = append(args, id)
	_, err := d.sql.Exec("UPDATE "+table+" SET "+strings.Join(sets, ", ")+" WHERE "+idCol+" = ?", args...)
	return err
}

// ---------------------------------------------------------------- settings

func (d *DB) GetSettings(def model.Settings) model.Settings {
	var raw string
	err := d.sql.QueryRow("SELECT value FROM settings WHERE key = 'global'").Scan(&raw)
	if err != nil || raw == "" {
		return def
	}
	var s model.Settings
	if json.Unmarshal([]byte(raw), &s) != nil {
		return def
	}
	// 环境变量里的映射根 / 请求头是「出厂默认」，用户没改过就跟随 env
	if s.StrmRoot == "" {
		s.StrmRoot = def.StrmRoot
	}
	if s.PublicBaseURL == "" {
		s.PublicBaseURL = def.PublicBaseURL
	}
	// 老库 JSON 里没有的新字段按默认值补齐。布尔字段尤其重要：
	// 缺键反序列化后是 false，会把「默认要二次确认」悄悄关掉。
	var keys map[string]json.RawMessage
	if json.Unmarshal([]byte(raw), &keys) == nil {
		for _, item := range []struct {
			key string
			set func()
		}{
			{"cleanup_max_delete_ratio", func() { s.CleanupMaxDeleteRatio = def.CleanupMaxDeleteRatio }},
			{"cleanup_stable_rounds", func() { s.CleanupStableRounds = def.CleanupStableRounds }},
			{"cleanup_confirm_required", func() { s.CleanupConfirmRequired = def.CleanupConfirmRequired }},
			{"cleanup_confirm_ttl_hours", func() { s.CleanupConfirmTTLHours = def.CleanupConfirmTTLHours }},
			{"default_concurrency", func() { s.DefaultConcurrency = def.DefaultConcurrency }},
		} {
			if _, ok := keys[item.key]; !ok {
				item.set()
			}
		}
	}
	return s
}

func (d *DB) SaveSettings(s model.Settings) error {
	raw, err := json.Marshal(s)
	if err != nil {
		return err
	}
	_, err = d.sql.Exec(`INSERT INTO settings(key, value) VALUES('global', ?)
		ON CONFLICT(key) DO UPDATE SET value = excluded.value`, string(raw))
	return err
}

func (d *DB) GetMeta(key, def string) string {
	var v string
	if err := d.sql.QueryRow("SELECT value FROM settings WHERE key = ?", key).Scan(&v); err != nil {
		return def
	}
	return v
}

func (d *DB) SetMeta(key, value string) error {
	_, err := d.sql.Exec(`INSERT INTO settings(key, value) VALUES(?, ?)
		ON CONFLICT(key) DO UPDATE SET value = excluded.value`, key, value)
	return err
}

// ---------------------------------------------------------------- accounts

func scanAccount(sc interface{ Scan(...any) error }) (*model.Account, error) {
	var a model.Account
	var cookies string
	var copyPlay, isDefault int
	err := sc.Scan(&a.ID, &a.Name, &a.UID, &a.UserName, &cookies, &a.Source,
		&copyPlay, &isDefault, &a.Status, &a.LastError, &a.CreatedAt, &a.UpdatedAt)
	if err != nil {
		return nil, err
	}
	a.HasCookies = cookies != ""
	a.CopyPlay = copyPlay == 1
	a.IsDefault = isDefault == 1
	return &a, nil
}

const accountCols = `id, name, uid, user_name, cookies, source, copy_play, is_default,
	status, last_error, created_at, updated_at`

func (d *DB) ListAccounts() []model.Account {
	rows, err := d.sql.Query("SELECT " + accountCols + " FROM accounts ORDER BY created_at ASC")
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.Account, 0, 4)
	for rows.Next() {
		if a, err := scanAccount(rows); err == nil {
			out = append(out, *a)
		}
	}
	return out
}

func (d *DB) GetAccount(id string) (*model.Account, error) {
	row := d.sql.QueryRow("SELECT "+accountCols+" FROM accounts WHERE id = ?", id)
	a, err := scanAccount(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return a, err
}

func (d *DB) GetAccountByUID(uid string) (*model.Account, error) {
	if uid == "" {
		return nil, nil
	}
	row := d.sql.QueryRow("SELECT "+accountCols+" FROM accounts WHERE uid = ? LIMIT 1", uid)
	a, err := scanAccount(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return a, err
}

func (d *DB) DefaultAccount() *model.Account {
	var id string
	err := d.sql.QueryRow("SELECT id FROM accounts WHERE is_default = 1 LIMIT 1").Scan(&id)
	if err != nil {
		err = d.sql.QueryRow("SELECT id FROM accounts ORDER BY created_at ASC LIMIT 1").Scan(&id)
		if err != nil {
			return nil
		}
	}
	a, _ := d.GetAccount(id)
	return a
}

func (d *DB) Cookies(id string) string {
	var c string
	_ = d.sql.QueryRow("SELECT cookies FROM accounts WHERE id = ?", id).Scan(&c)
	return c
}

func (d *DB) UpsertAccount(a model.Account, cookies string) error {
	ts := now()
	if a.CreatedAt == 0 {
		a.CreatedAt = ts
	}
	copyPlay := 0
	if a.CopyPlay {
		copyPlay = 1
	}
	isDefault := 0
	if a.IsDefault {
		isDefault = 1
	}
	_, err := d.sql.Exec(`INSERT INTO accounts
		(id, name, uid, user_name, cookies, source, copy_play, is_default, status, last_error, created_at, updated_at)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(id) DO UPDATE SET
		  name = excluded.name, uid = excluded.uid, user_name = excluded.user_name,
		  cookies = CASE WHEN excluded.cookies = '' THEN accounts.cookies ELSE excluded.cookies END,
		  source = excluded.source, copy_play = excluded.copy_play,
		  status = excluded.status, last_error = excluded.last_error,
		  updated_at = excluded.updated_at`,
		a.ID, a.Name, a.UID, a.UserName, cookies, a.Source, copyPlay, isDefault,
		a.Status, a.LastError, a.CreatedAt, ts)
	return err
}

func (d *DB) UpdateAccount(id string, fields map[string]any) error {
	fields["updated_at"] = now()
	return d.update("accounts", "id", id, fields)
}

func (d *DB) SetDefaultAccount(id string) error {
	if _, err := d.sql.Exec("UPDATE accounts SET is_default = 0"); err != nil {
		return err
	}
	_, err := d.sql.Exec("UPDATE accounts SET is_default = 1 WHERE id = ?", id)
	return err
}

func (d *DB) DeleteAccount(id string) error {
	_, err := d.sql.Exec("DELETE FROM accounts WHERE id = ?", id)
	return err
}

// ---------------------------------------------------------------- configs

const configCols = `id, name, account_id, remote_id, remote_path, local_dir, mode,
	include_exts, all_files, watch_enabled, watch_delay, watch_interval, last_sig,
	last_check_at, sched_enabled, sched_minutes, download_meta, last_run_at, last_status, last_message,
	files, created_at, paths, strm_base, meta_exts,
	overwrite_mode, exclude_keywords, min_file_size_mb, skip_pending, pending_dirs,
	concurrency, cron_full, cleanup_stale_sig, cleanup_stale_count`

func scanConfig(sc interface{ Scan(...any) error }) (*model.StrmConfig, error) {
	var c model.StrmConfig
	var allFiles, watch, sched, meta, skipPending int
	var pathsJSON string
	err := sc.Scan(&c.ID, &c.Name, &c.AccountID, &c.RemoteID, &c.RemotePath, &c.LocalDir,
		&c.Mode, &c.IncludeExts, &allFiles, &watch, &c.WatchDelaySec, &c.WatchInterval,
		&c.LastSig, &c.LastCheckAt, &sched, &c.SchedMinutes, &meta, &c.LastRunAt, &c.LastStatus,
		&c.LastMessage, &c.Files, &c.CreatedAt,
		&pathsJSON, &c.StrmBase, &c.MetaExts,
		&c.OverwriteMode, &c.ExcludeKeywords, &c.MinFileSizeMB, &skipPending, &c.PendingDirs,
		&c.Concurrency, &c.CronFull, &c.CleanupStaleSig, &c.CleanupStaleCount)
	if err != nil {
		return nil, err
	}
	c.AllFiles = allFiles == 1
	c.WatchEnabled = watch == 1
	c.SchedEnabled = sched == 1
	c.DownloadMeta = meta == 1
	c.SkipPending = skipPending == 1
	if strings.TrimSpace(c.OverwriteMode) == "" {
		c.OverwriteMode = "always"
	}
	if strings.TrimSpace(pathsJSON) != "" {
		_ = json.Unmarshal([]byte(pathsJSON), &c.Paths)
	}
	return &c, nil
}

func (d *DB) ListConfigs() []model.StrmConfig {
	rows, err := d.sql.Query("SELECT " + configCols + " FROM strm_configs ORDER BY created_at ASC")
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.StrmConfig, 0, 8)
	for rows.Next() {
		if c, err := scanConfig(rows); err == nil {
			out = append(out, *c)
		}
	}
	return out
}

func (d *DB) GetConfig(id string) (*model.StrmConfig, error) {
	row := d.sql.QueryRow("SELECT "+configCols+" FROM strm_configs WHERE id = ?", id)
	c, err := scanConfig(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return c, err
}

func (d *DB) CreateConfig(c model.StrmConfig) error {
	if c.CreatedAt == 0 {
		c.CreatedAt = now()
	}
	b := func(v bool) int {
		if v {
			return 1
		}
		return 0
	}
	pathsJSON := ""
	if len(c.Paths) > 0 {
		if b, err := json.Marshal(c.Paths); err == nil {
			pathsJSON = string(b)
		}
	}
	overwrite := c.OverwriteMode
	if strings.TrimSpace(overwrite) == "" {
		overwrite = "always"
	}
	_, err := d.sql.Exec(`INSERT INTO strm_configs (`+configCols+`)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		c.ID, c.Name, c.AccountID, c.RemoteID, c.RemotePath, c.LocalDir, c.Mode,
		c.IncludeExts, b(c.AllFiles), b(c.WatchEnabled), c.WatchDelaySec,
		c.WatchInterval, c.LastSig, c.LastCheckAt, b(c.SchedEnabled), c.SchedMinutes,
		b(c.DownloadMeta), c.LastRunAt, c.LastStatus, c.LastMessage, c.Files, c.CreatedAt,
		pathsJSON, c.StrmBase, c.MetaExts,
		overwrite, c.ExcludeKeywords, c.MinFileSizeMB, b(c.SkipPending), c.PendingDirs,
		c.Concurrency, c.CronFull, c.CleanupStaleSig, c.CleanupStaleCount)
	return err
}

func (d *DB) UpdateConfig(id string, fields map[string]any) error {
	return d.update("strm_configs", "id", id, fields)
}

func (d *DB) DeleteConfig(id string) error {
	_, err := d.sql.Exec("DELETE FROM strm_configs WHERE id = ?", id)
	if err == nil {
		_, err = d.sql.Exec("DELETE FROM strm_files WHERE config_id = ?", id)
	}
	return err
}

// ---------------------------------------------------------------- files

func (d *DB) UpsertFile(f model.StrmFile) error {
	if f.CreatedAt == 0 {
		f.CreatedAt = now()
	}
	_, err := d.sql.Exec(`INSERT INTO strm_files
		(config_id, account_id, file_id, pickcode, name, rel_path, strm_path, size,
		 sha1, parent_id, share_code, source, deleted, created_at)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
		ON CONFLICT(config_id, file_id) DO UPDATE SET
		  account_id = excluded.account_id, pickcode = excluded.pickcode,
		  name = excluded.name, rel_path = excluded.rel_path,
		  strm_path = excluded.strm_path, size = excluded.size,
		  sha1 = excluded.sha1, parent_id = excluded.parent_id,
		  share_code = excluded.share_code, source = excluded.source, deleted = 0`,
		f.ConfigID, f.AccountID, f.FileID, f.Pickcode, f.Name, f.RelPath, f.StrmPath,
		f.Size, f.Sha1, f.ParentID, f.ShareCode, f.Source, f.CreatedAt)
	return err
}

func (d *DB) ListFiles(configID string) []model.StrmFile {
	rows, err := d.sql.Query(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE config_id = ?`, configID)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.StrmFile, 0, 256)
	for rows.Next() {
		var f model.StrmFile
		var deleted int
		if err := rows.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode,
			&f.Name, &f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID,
			&f.ShareCode, &f.Source, &deleted, &f.CreatedAt); err == nil {
			f.Deleted = deleted == 1
			out = append(out, f)
		}
	}
	return out
}

func (d *DB) CountFiles(configID string) int {
	var n int
	_ = d.sql.QueryRow("SELECT COUNT(*) FROM strm_files WHERE config_id = ? AND deleted = 0", configID).Scan(&n)
	return n
}

func (d *DB) FindFileByPickcode(pickcode string) (*model.StrmFile, error) {
	row := d.sql.QueryRow(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE pickcode = ? AND deleted = 0 LIMIT 1`, pickcode)
	var f model.StrmFile
	var deleted int
	err := row.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode, &f.Name,
		&f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID, &f.ShareCode,
		&f.Source, &deleted, &f.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	f.Deleted = deleted == 1
	return &f, err
}

func (d *DB) FindFileByStrmPath(path string) (*model.StrmFile, error) {
	row := d.sql.QueryRow(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE strm_path = ? LIMIT 1`, path)
	var f model.StrmFile
	var deleted int
	err := row.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode, &f.Name,
		&f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID, &f.ShareCode,
		&f.Source, &deleted, &f.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	f.Deleted = deleted == 1
	return &f, err
}

func (d *DB) FindFileByRelPath(relPath string) (*model.StrmFile, error) {
	row := d.sql.QueryRow(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE rel_path = ? AND deleted = 0 LIMIT 1`, relPath)
	var f model.StrmFile
	var deleted int
	err := row.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode, &f.Name,
		&f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID, &f.ShareCode,
		&f.Source, &deleted, &f.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	f.Deleted = deleted == 1
	return &f, err
}

func (d *DB) FindFileBySha1(sha1 string, accountID string) (*model.StrmFile, error) {
	row := d.sql.QueryRow(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE sha1 = ? AND account_id = ? AND deleted = 0 LIMIT 1`, sha1, accountID)
	var f model.StrmFile
	var deleted int
	err := row.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode, &f.Name,
		&f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID, &f.ShareCode,
		&f.Source, &deleted, &f.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	f.Deleted = deleted == 1
	return &f, err
}

// FindFileByFileID 按「配置 + 115 文件 id」查索引行，事件监控增删改都靠它定位。
func (d *DB) FindFileByFileID(configID, fileID string) (*model.StrmFile, error) {
	row := d.sql.QueryRow(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE config_id = ? AND file_id = ? LIMIT 1`, configID, fileID)
	var f model.StrmFile
	var deleted int
	err := row.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode, &f.Name,
		&f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID, &f.ShareCode,
		&f.Source, &deleted, &f.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	f.Deleted = deleted == 1
	return &f, err
}

func (d *DB) DeleteFileRow(id int64) error {
	_, err := d.sql.Exec("DELETE FROM strm_files WHERE id = ?", id)
	return err
}

// DeleteFileRowsByStrmPath 清掉同一条 strm 路径上的旧索引行。
// 同名文件被重新复制/上传时 file_id 变了，不清就会留下指向同一个 strm 的僵尸行。
func (d *DB) DeleteFileRowsByStrmPath(configID, strmPath string) error {
	if strmPath == "" {
		return nil
	}
	_, err := d.sql.Exec("DELETE FROM strm_files WHERE config_id = ? AND strm_path = ? AND file_id <> ''", configID, strmPath)
	return err
}

func (d *DB) DeleteFileByFileID(configID, fileID string) error {
	_, err := d.sql.Exec("DELETE FROM strm_files WHERE config_id = ? AND file_id = ?", configID, fileID)
	return err
}

func (d *DB) ClearFiles(configID string) error {
	_, err := d.sql.Exec("DELETE FROM strm_files WHERE config_id = ?", configID)
	return err
}

func (d *DB) SearchFiles(q string, limit int) []model.StrmFile {
	if limit <= 0 {
		limit = 100
	}
	rows, err := d.sql.Query(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE deleted = 0 AND (name LIKE ? OR rel_path LIKE ?)
		ORDER BY id DESC LIMIT ?`, "%"+q+"%", "%"+q+"%", limit)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.StrmFile, 0, limit)
	for rows.Next() {
		var f model.StrmFile
		var deleted int
		if err := rows.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode,
			&f.Name, &f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID,
			&f.ShareCode, &f.Source, &deleted, &f.CreatedAt); err == nil {
			f.Deleted = deleted == 1
			out = append(out, f)
		}
	}
	return out
}

// ---------------------------------------------------------------- share tasks

const shareCols = `id, account_id, url, share_code, receive_code, title, dir_cid, dir_name,
	local_dir, no_transfer, my_share_code, my_receive_code, status, message, files, created_at, updated_at`

func scanShare(sc interface{ Scan(...any) error }) (*model.ShareTask, error) {
	var t model.ShareTask
	var noTransfer int
	err := sc.Scan(&t.ID, &t.AccountID, &t.URL, &t.ShareCode, &t.ReceiveCode, &t.Title,
		&t.DirCID, &t.DirName, &t.LocalDir, &noTransfer, &t.MyShareCode, &t.MyReceiveCode,
		&t.Status, &t.Message, &t.Files, &t.CreatedAt, &t.UpdatedAt)
	if err != nil {
		return nil, err
	}
	t.NoTransfer = noTransfer == 1
	return &t, nil
}

func (d *DB) ListShares() []model.ShareTask {
	rows, err := d.sql.Query("SELECT " + shareCols + " FROM share_tasks ORDER BY created_at DESC")
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.ShareTask, 0, 16)
	for rows.Next() {
		if t, err := scanShare(rows); err == nil {
			out = append(out, *t)
		}
	}
	return out
}

func (d *DB) GetShare(id string) (*model.ShareTask, error) {
	row := d.sql.QueryRow("SELECT "+shareCols+" FROM share_tasks WHERE id = ?", id)
	t, err := scanShare(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return t, err
}

func (d *DB) SaveShare(t model.ShareTask) error {
	b := 0
	if t.NoTransfer {
		b = 1
	}
	if t.CreatedAt == 0 {
		t.CreatedAt = now()
	}
	_, err := d.sql.Exec(`INSERT INTO share_tasks (`+shareCols+`)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(id) DO UPDATE SET
		  title = excluded.title, dir_cid = excluded.dir_cid, dir_name = excluded.dir_name,
		  local_dir = excluded.local_dir, no_transfer = excluded.no_transfer,
		  my_share_code = excluded.my_share_code, my_receive_code = excluded.my_receive_code,
		  status = excluded.status, message = excluded.message, files = excluded.files,
		  updated_at = excluded.updated_at`,
		t.ID, t.AccountID, t.URL, t.ShareCode, t.ReceiveCode, t.Title, t.DirCID,
		t.DirName, t.LocalDir, b, t.MyShareCode, t.MyReceiveCode, t.Status, t.Message,
		t.Files, t.CreatedAt, now())
	return err
}

func (d *DB) UpdateShare(id string, fields map[string]any) error {
	fields["updated_at"] = now()
	return d.update("share_tasks", "id", id, fields)
}

func (d *DB) DeleteShare(id string) error {
	_, err := d.sql.Exec("DELETE FROM share_tasks WHERE id = ?", id)
	return err
}

// ---------------------------------------------------------------- ed2k

const ed2kCols = `id, name, size, url, account_id, dir_cid, local_dir, transfer_on_add,
	state, message, pickcode, file_id, created_at, updated_at`

func scanEd2k(sc interface{ Scan(...any) error }) (*model.Ed2kTask, error) {
	var t model.Ed2kTask
	var onAdd int
	err := sc.Scan(&t.ID, &t.Name, &t.Size, &t.URL, &t.AccountID, &t.DirCID, &t.LocalDir,
		&onAdd, &t.State, &t.Message, &t.Pickcode, &t.FileID, &t.CreatedAt, &t.UpdatedAt)
	if err != nil {
		return nil, err
	}
	t.TransferOnAdd = onAdd == 1
	return &t, nil
}

func (d *DB) ListEd2k() []model.Ed2kTask {
	rows, err := d.sql.Query("SELECT " + ed2kCols + " FROM ed2k_tasks ORDER BY created_at DESC")
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.Ed2kTask, 0, 16)
	for rows.Next() {
		if t, err := scanEd2k(rows); err == nil {
			out = append(out, *t)
		}
	}
	return out
}

func (d *DB) GetEd2k(id string) (*model.Ed2kTask, error) {
	row := d.sql.QueryRow("SELECT "+ed2kCols+" FROM ed2k_tasks WHERE id = ?", id)
	t, err := scanEd2k(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return t, err
}

func (d *DB) SaveEd2k(t model.Ed2kTask) error {
	b := 0
	if t.TransferOnAdd {
		b = 1
	}
	ts := now()
	if t.CreatedAt == 0 {
		t.CreatedAt = ts
	}
	_, err := d.sql.Exec(`INSERT INTO ed2k_tasks (`+ed2kCols+`)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(id) DO UPDATE SET
		  name = excluded.name, size = excluded.size, url = excluded.url,
		  account_id = excluded.account_id, dir_cid = excluded.dir_cid,
		  local_dir = excluded.local_dir, transfer_on_add = excluded.transfer_on_add,
		  state = excluded.state, message = excluded.message,
		  pickcode = excluded.pickcode, file_id = excluded.file_id,
		  updated_at = excluded.updated_at`,
		t.ID, t.Name, t.Size, t.URL, t.AccountID, t.DirCID, t.LocalDir, b, t.State,
		t.Message, t.Pickcode, t.FileID, t.CreatedAt, ts)
	return err
}

func (d *DB) UpdateEd2k(id string, fields map[string]any) error {
	fields["updated_at"] = now()
	return d.update("ed2k_tasks", "id", id, fields)
}

func (d *DB) DeleteEd2k(id string) error {
	_, err := d.sql.Exec("DELETE FROM ed2k_tasks WHERE id = ?", id)
	return err
}

// ---------------------------------------------------------------- instant

func (d *DB) ListInstantTargets() []model.InstantTarget {
	rows, err := d.sql.Query(`SELECT t.account_id, COALESCE(a.name,''), t.dir_cid, t.dir_path, t.enabled
		FROM instant_targets t LEFT JOIN accounts a ON a.id = t.account_id
		ORDER BY t.account_id`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.InstantTarget, 0, 4)
	for rows.Next() {
		var t model.InstantTarget
		var enabled int
		if err := rows.Scan(&t.AccountID, &t.AccountName, &t.DirCID, &t.DirPath, &enabled); err == nil {
			t.Enabled = enabled == 1
			out = append(out, t)
		}
	}
	return out
}

func (d *DB) SaveInstantTarget(t model.InstantTarget) error {
	e := 0
	if t.Enabled {
		e = 1
	}
	_, err := d.sql.Exec(`INSERT INTO instant_targets(account_id, dir_cid, dir_path, enabled)
		VALUES (?,?,?,?)
		ON CONFLICT(account_id) DO UPDATE SET dir_cid = excluded.dir_cid,
		  dir_path = excluded.dir_path, enabled = excluded.enabled`,
		t.AccountID, t.DirCID, t.DirPath, e)
	return err
}

func (d *DB) DeleteInstantTarget(accountID string) error {
	_, err := d.sql.Exec("DELETE FROM instant_targets WHERE account_id = ?", accountID)
	return err
}

func (d *DB) ListInstantMap() []model.InstantMapping {
	rows, err := d.sql.Query(`SELECT m.emby_user_id, m.emby_user_name, m.account_id,
		COALESCE(a.name,''), m.updated_at FROM instant_map m
		LEFT JOIN accounts a ON a.id = m.account_id ORDER BY m.updated_at DESC`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.InstantMapping, 0, 8)
	for rows.Next() {
		var m model.InstantMapping
		if err := rows.Scan(&m.EmbyUserID, &m.EmbyUserName, &m.AccountID, &m.AccountName,
			&m.UpdatedAt); err == nil {
			out = append(out, m)
		}
	}
	return out
}

func (d *DB) SaveInstantMap(m model.InstantMapping) error {
	_, err := d.sql.Exec(`INSERT INTO instant_map(emby_user_id, emby_user_name, account_id, updated_at)
		VALUES (?,?,?,?)
		ON CONFLICT(emby_user_id) DO UPDATE SET emby_user_name = excluded.emby_user_name,
		  account_id = excluded.account_id, updated_at = excluded.updated_at`,
		m.EmbyUserID, m.EmbyUserName, m.AccountID, now())
	return err
}

func (d *DB) DeleteInstantMap(userID string) error {
	_, err := d.sql.Exec("DELETE FROM instant_map WHERE emby_user_id = ?", userID)
	return err
}

func (d *DB) InstantAccountForUser(userID string) string {
	var acc string
	_ = d.sql.QueryRow("SELECT account_id FROM instant_map WHERE emby_user_id = ?", userID).Scan(&acc)
	return acc
}

// ---------------------------------------------------------------- emby

const embyCols = `id, name, url, api_key, enabled, created_at`

func (d *DB) ListEmbyServers() []model.EmbyServer {
	rows, err := d.sql.Query(`SELECT ` + embyCols + ` FROM emby_servers ORDER BY created_at ASC`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.EmbyServer, 0, 4)
	for rows.Next() {
		var s model.EmbyServer
		var enabled int
		if err := rows.Scan(&s.ID, &s.Name, &s.URL, &s.APIKey, &enabled, &s.CreatedAt); err == nil {
			s.Enabled = enabled == 1
			out = append(out, s)
		}
	}
	// 反代默认使用第一台启用的服务器
	for i := range out {
		if out[i].Enabled {
			out[i].IsDefault = true
			break
		}
	}
	return out
}

func (d *DB) GetEmbyServer(id string) (*model.EmbyServer, error) {
	var s model.EmbyServer
	var enabled int
	err := d.sql.QueryRow("SELECT "+embyCols+" FROM emby_servers WHERE id = ?", id).
		Scan(&s.ID, &s.Name, &s.URL, &s.APIKey, &enabled, &s.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	s.Enabled = enabled == 1
	return &s, nil
}

func (d *DB) SaveEmbyServer(s model.EmbyServer) error {
	e := 0
	if s.Enabled {
		e = 1
	}
	if s.CreatedAt == 0 {
		s.CreatedAt = now()
	}
	_, err := d.sql.Exec(`INSERT INTO emby_servers (`+embyCols+`)
		VALUES (?,?,?,?,?,?)
		ON CONFLICT(id) DO UPDATE SET name = excluded.name, url = excluded.url,
		  api_key = CASE WHEN excluded.api_key = '' THEN emby_servers.api_key ELSE excluded.api_key END,
		  enabled = excluded.enabled`,
		s.ID, s.Name, s.URL, s.APIKey, e, s.CreatedAt)
	return err
}

func (d *DB) UpdateEmbyServer(id string, fields map[string]any) error {
	return d.update("emby_servers", "id", id, fields)
}

func (d *DB) DeleteEmbyServer(id string) error {
	_, err := d.sql.Exec("DELETE FROM emby_servers WHERE id = ?", id)
	return err
}

func (d *DB) EmbyByAPIKey(key string) (*model.EmbyServer, error) {
	var id string
	if err := d.sql.QueryRow("SELECT id FROM emby_servers WHERE api_key = ? LIMIT 1", key).Scan(&id); err != nil {
		return nil, nil
	}
	return d.GetEmbyServer(id)
}

// ---------------------------------------------------------------- events

func (d *DB) AddEvent(e model.Event) int64 {
	if e.CreatedAt == 0 {
		e.CreatedAt = now()
	}
	res, err := d.sql.Exec(`INSERT INTO events(level, kind, message, detail, created_at)
		VALUES (?,?,?,?,?)`, e.Level, e.Kind, e.Message, e.Detail, e.CreatedAt)
	if err != nil {
		return 0
	}
	id, _ := res.LastInsertId()
	if n := d.countRows("events"); n > 5000 {
		_, _ = d.sql.Exec(`DELETE FROM events WHERE id IN
			(SELECT id FROM events ORDER BY id ASC LIMIT 1000)`)
	}
	return id
}

func (d *DB) countRows(table string) int {
	var n int
	_ = d.sql.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n)
	return n
}

func (d *DB) ListEvents(limit int) []model.Event {
	if limit <= 0 {
		limit = 200
	}
	rows, err := d.sql.Query(`SELECT id, level, kind, message, detail, created_at
		FROM events ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.Event, 0, limit)
	for rows.Next() {
		var e model.Event
		if err := rows.Scan(&e.ID, &e.Level, &e.Kind, &e.Message, &e.Detail, &e.CreatedAt); err == nil {
			out = append(out, e)
		}
	}
	return out
}

func (d *DB) ClearEvents() error {
	_, err := d.sql.Exec("DELETE FROM events")
	return err
}

// ---------------------------------------------------------------- deletions

const deletionCols = `id, kind, account_id, file_id, path, strm_path, state, message, created_at`

func scanDeletion(sc interface{ Scan(...any) error }) (*model.Deletion, error) {
	var x model.Deletion
	err := sc.Scan(&x.ID, &x.Kind, &x.AccountID, &x.FileID, &x.Path, &x.StrmPath,
		&x.State, &x.Message, &x.CreatedAt)
	if err != nil {
		return nil, err
	}
	return &x, nil
}

func (d *DB) AddDeletion(x model.Deletion) error {
	if x.CreatedAt == 0 {
		x.CreatedAt = now()
	}
	_, err := d.sql.Exec(`INSERT INTO deletions(kind, account_id, file_id, path, strm_path, state, message, created_at)
		VALUES (?,?,?,?,?,?,?,?)`, x.Kind, x.AccountID, x.FileID, x.Path, x.StrmPath, x.State, x.Message, x.CreatedAt)
	return err
}

func (d *DB) ListDeletions() []model.Deletion {
	rows, err := d.sql.Query("SELECT " + deletionCols + " FROM deletions ORDER BY id DESC LIMIT 500")
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.Deletion, 0, 32)
	for rows.Next() {
		if x, err := scanDeletion(rows); err == nil {
			out = append(out, *x)
		}
	}
	return out
}

func (d *DB) GetDeletion(id int64) (*model.Deletion, error) {
	row := d.sql.QueryRow("SELECT "+deletionCols+" FROM deletions WHERE id = ?", id)
	x, err := scanDeletion(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return x, err
}

func (d *DB) UpdateDeletion(id int64, fields map[string]any) error {
	return d.update("deletions", "id", fmt.Sprint(id), fields)
}

func (d *DB) DeleteDeletion(id int64) error {
	_, err := d.sql.Exec("DELETE FROM deletions WHERE id = ?", id)
	return err
}

// FindShareByMyCode 按「自己的分享码」反查任务（播放 /p115/share/<code>/... 时用）。
func (d *DB) FindShareByMyCode(code string) (*model.ShareTask, error) {
	row := d.sql.QueryRow("SELECT "+shareCols+" FROM share_tasks WHERE my_share_code = ? LIMIT 1", code)
	t, err := scanShare(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return t, err
}

// FindShareByCode 按源分享码反查任务。
func (d *DB) FindShareByCode(code string) (*model.ShareTask, error) {
	row := d.sql.QueryRow("SELECT "+shareCols+" FROM share_tasks WHERE share_code = ? LIMIT 1", code)
	t, err := scanShare(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return t, err
}

// FindFileBySuffix 按路径后缀匹配索引（Emby 报的是它自己容器里的绝对路径，
// 我们只认识映射根下的相对路径，所以用后缀对齐）。
func (d *DB) FindFileBySuffix(p string) (*model.StrmFile, error) {
	p = strings.ReplaceAll(p, "\\", "/")
	row := d.sql.QueryRow(`SELECT id, config_id, account_id, file_id, pickcode, name,
		rel_path, strm_path, size, sha1, parent_id, share_code, source, deleted, created_at
		FROM strm_files WHERE deleted = 0
		  AND (? = strm_path OR ? LIKE '%' || rel_path || '.strm' OR ? = rel_path)
		LIMIT 1`, p, p, p)
	var f model.StrmFile
	var deleted int
	err := row.Scan(&f.ID, &f.ConfigID, &f.AccountID, &f.FileID, &f.Pickcode, &f.Name,
		&f.RelPath, &f.StrmPath, &f.Size, &f.Sha1, &f.ParentID, &f.ShareCode,
		&f.Source, &deleted, &f.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	f.Deleted = deleted == 1
	return &f, nil
}

// ---------------------------------------------------------------- 批量写索引

// UpsertFiles 在一个事务里批量写索引。并发生成时 worker 只负责扫目录和写 strm，
// 索引攒成一批再落库，避免每行一次 fsync。
func (d *DB) UpsertFiles(files []model.StrmFile) error {
	if len(files) == 0 {
		return nil
	}
	tx, err := d.sql.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT INTO strm_files
		(config_id, account_id, file_id, pickcode, name, rel_path, strm_path, size,
		 sha1, parent_id, share_code, source, deleted, created_at)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
		ON CONFLICT(config_id, file_id) DO UPDATE SET
		  account_id = excluded.account_id, pickcode = excluded.pickcode,
		  name = excluded.name, rel_path = excluded.rel_path,
		  strm_path = excluded.strm_path, size = excluded.size,
		  sha1 = excluded.sha1, parent_id = excluded.parent_id,
		  share_code = excluded.share_code, source = excluded.source, deleted = 0`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, f := range files {
		if f.CreatedAt == 0 {
			f.CreatedAt = now()
		}
		if _, err := stmt.Exec(f.ConfigID, f.AccountID, f.FileID, f.Pickcode, f.Name,
			f.RelPath, f.StrmPath, f.Size, f.Sha1, f.ParentID, f.ShareCode,
			f.Source, f.CreatedAt); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// DeleteFileRows 批量删除索引行（清理确认通过后调用）。
func (d *DB) DeleteFileRows(configID string, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	tx, err := d.sql.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare("DELETE FROM strm_files WHERE config_id = ? AND file_id = ?")
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, id := range ids {
		if _, err := stmt.Exec(configID, id); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// ---------------------------------------------------------------- 清理确认队列

const cleanupCols = `id, config_id, config_name, stale_sig, items, total, ratio, state, created_at, expires_at`

func scanCleanup(sc interface{ Scan(...any) error }) (*model.CleanupConfirmation, error) {
	var x model.CleanupConfirmation
	var items string
	err := sc.Scan(&x.ID, &x.ConfigID, &x.ConfigName, &x.StaleSig, &items,
		&x.Total, &x.Ratio, &x.State, &x.CreatedAt, &x.ExpiresAt)
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(items), &x.Items)
	return &x, nil
}

func (d *DB) CreateCleanup(x model.CleanupConfirmation) error {
	items, err := json.Marshal(x.Items)
	if err != nil {
		return err
	}
	_, err = d.sql.Exec(`INSERT INTO cleanup_confirmations (`+cleanupCols+`)
		VALUES (?,?,?,?,?,?,?,?,?,?)`, x.ID, x.ConfigID, x.ConfigName, x.StaleSig,
		string(items), x.Total, x.Ratio, x.State, x.CreatedAt, x.ExpiresAt)
	return err
}

func (d *DB) ListCleanups(state string) []model.CleanupConfirmation {
	rows, err := d.sql.Query("SELECT "+cleanupCols+" FROM cleanup_confirmations WHERE state = ? ORDER BY created_at DESC", state)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := make([]model.CleanupConfirmation, 0, 8)
	for rows.Next() {
		if x, err := scanCleanup(rows); err == nil {
			out = append(out, *x)
		}
	}
	return out
}

func (d *DB) GetCleanup(id string) (*model.CleanupConfirmation, error) {
	row := d.sql.QueryRow("SELECT "+cleanupCols+" FROM cleanup_confirmations WHERE id = ?", id)
	x, err := scanCleanup(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return x, err
}

func (d *DB) SetCleanupState(id, state string) error {
	_, err := d.sql.Exec("UPDATE cleanup_confirmations SET state = ? WHERE id = ?", state, id)
	return err
}

// ExpireCleanups 把过期的 pending 计划标记为 expired，列表接口会再过滤一层。
func (d *DB) ExpireCleanups() {
	_, _ = d.sql.Exec("UPDATE cleanup_confirmations SET state = 'expired' WHERE state = 'pending' AND expires_at > 0 AND expires_at < ?", now())
}
