package db

import "time"

// 秒传二次校验的数据块缓存。
//
// 115 秒传大于 1MB 的文件时要「二次校验」：给一个字节区间，让我们把那一段
// 的内容 sha1 回传。这段字节只跟文件内容有关，跟哪个账号、第几次播放都无关，
// 所以按 (sha1, 块起点) 缓存起来 —— 第二个人播同一部片子就不用再回源盘取。

// DigestStats 是校验缓存的占用情况（页面上要显示）。
type DigestStats struct {
	Blocks int64 `json:"blocks"`
	Bytes  int64 `json:"bytes"`
}

// DigestBlockGet 取一块校验数据；没有就返回 nil。
func (d *DB) DigestBlockGet(sha1 string, off int64) []byte {
	if sha1 == "" {
		return nil
	}
	var data []byte
	err := d.sql.QueryRow(
		"SELECT data FROM instant_digest WHERE sha1 = ? AND off = ?", sha1, off,
	).Scan(&data)
	if err != nil || len(data) == 0 {
		return nil
	}
	// 记一次命中时间，清理时按「最久没用」丢
	_, _ = d.sql.Exec(
		"UPDATE instant_digest SET used_at = ? WHERE sha1 = ? AND off = ?",
		time.Now().Unix(), sha1, off)
	return data
}

// DigestBlockPut 存一块校验数据。
func (d *DB) DigestBlockPut(sha1 string, off int64, data []byte) error {
	if sha1 == "" || len(data) == 0 {
		return nil
	}
	_, err := d.sql.Exec(`INSERT INTO instant_digest (sha1, off, size, data, used_at)
		VALUES (?,?,?,?,?)
		ON CONFLICT(sha1, off) DO UPDATE SET
		  size = excluded.size, data = excluded.data, used_at = excluded.used_at`,
		sha1, off, len(data), data, time.Now().Unix())
	return err
}

// DigestStats 返回块数与总字节数。
func (d *DB) DigestStats() DigestStats {
	var s DigestStats
	_ = d.sql.QueryRow(
		"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM instant_digest",
	).Scan(&s.Blocks, &s.Bytes)
	return s
}

// DigestClear 清空校验缓存，返回清掉的块数。
func (d *DB) DigestClear() int64 {
	before := d.DigestStats().Blocks
	_, _ = d.sql.Exec("DELETE FROM instant_digest")
	return before
}

// DigestSweep 把缓存压到 maxBytes 以内：先丢最久没用过的。
// maxBytes <= 0 表示不限制，直接返回。
func (d *DB) DigestSweep(maxBytes int64) (dropped, freed int64) {
	if maxBytes <= 0 {
		return 0, 0
	}
	st := d.DigestStats()
	if st.Bytes <= maxBytes {
		return 0, 0
	}
	rows, err := d.sql.Query(
		"SELECT sha1, off, size FROM instant_digest ORDER BY used_at ASC")
	if err != nil {
		return 0, 0
	}
	type key struct {
		sha1 string
		off  int64
	}
	var victims []key
	keepBytes := st.Bytes
	for rows.Next() {
		var k key
		var size int64
		if err := rows.Scan(&k.sha1, &k.off, &size); err != nil {
			continue
		}
		if keepBytes <= maxBytes {
			continue
		}
		victims = append(victims, k)
		keepBytes -= size
		freed += size
	}
	rows.Close()
	for _, k := range victims {
		if _, err := d.sql.Exec(
			"DELETE FROM instant_digest WHERE sha1 = ? AND off = ?", k.sha1, k.off,
		); err == nil {
			dropped++
		}
	}
	return dropped, freed
}
