package p115

import "testing"

// 老离线网关用数字 status：2 完成、3 失败、0/1 在跑。
func TestNormalizeOfflineStatus(t *testing.T) {
	cases := []struct {
		status map[string]any
		want   string
	}{
		{map[string]any{"status": float64(2), "percentDone": float64(42)}, "done"},
		{map[string]any{"status": float64(3)}, "failed"},
		{map[string]any{"status": float64(1), "percentDone": float64(37)}, "running"},
		{map[string]any{"status": float64(0)}, "running"},
		{map[string]any{"display_status": "finished"}, "done"},
		{map[string]any{"status_text": "下载失败"}, "failed"},
	}
	for _, c := range cases {
		it := map[string]any{"info_hash": "ABC", "name": "x.mkv", "size": float64(100)}
		for k, v := range c.status {
			it[k] = v
		}
		got := normalizeOffline(it, false)
		if got.Status != c.want {
			t.Fatalf("%v → %q，应为 %q", c.status, got.Status, c.want)
		}
		if got.InfoHash != "abc" {
			t.Fatalf("info_hash 应转小写，得到 %q", got.InfoHash)
		}
		if c.want == "done" && got.Percent != 100 {
			t.Fatalf("完成的任务进度应为 100，得到 %d", got.Percent)
		}
	}
}
