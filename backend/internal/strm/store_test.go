package strm

import (
	"os"
	"path/filepath"
	"testing"
)

func newStore(t *testing.T) *Store {
	t.Helper()
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return s
}

// 映射根之外一个字节都不能写：strm 路径来自网盘文件名，脏名字不能越界。
func TestStoreStaysInsideRoot(t *testing.T) {
	s := newStore(t)
	for _, rel := range []string{
		"../../etc/passwd",
		"../outside.strm",
		"/../../tmp/evil.strm",
		"国漫/../../外面.strm",
	} {
		abs, err := s.Abs(rel)
		if err != nil {
			continue // 直接拒绝也可以
		}
		if abs != s.Root && !isUnder(abs, s.Root) {
			t.Fatalf("Abs(%q) 跑出了映射根: %s", rel, abs)
		}
	}
}

func isUnder(p, root string) bool {
	rel, err := filepath.Rel(root, p)
	if err != nil {
		return false
	}
	return rel != ".." && !filepath.IsAbs(rel) && (len(rel) < 2 || rel[:2] != "..")
}

func TestStoreWriteSkipsSameContent(t *testing.T) {
	s := newStore(t)
	rel := "国漫/斗罗大陆/01.strm"
	content := "http://127.0.0.1:9527/p115/play/pc1/01.mkv/111"

	abs, changed, err := s.Write(rel, content)
	if err != nil {
		t.Fatalf("Write: %v", err)
	}
	if !changed {
		t.Fatal("首次写应该 changed")
	}
	if filepath.Ext(abs) != ".strm" {
		t.Fatalf("写入文件应该带 .strm 后缀: %s", abs)
	}
	if got := s.Read(abs); got != content {
		t.Fatalf("读回来不一致: %q", got)
	}

	if _, changed, err := s.Write(rel, content); err != nil || changed {
		t.Fatalf("内容相同应跳过，changed=%v err=%v", changed, err)
	}
	if _, changed, err := s.Write(rel, content+"?v=2"); err != nil || !changed {
		t.Fatalf("内容变了应该重写，changed=%v err=%v", changed, err)
	}
	if got := s.Read(abs); got != content+"?v=2" {
		t.Fatalf("重写后内容不对: %q", got)
	}
}

func TestSanitizeAndExts(t *testing.T) {
	if got := Sanitize(`a/b:c*d?"e<f>g|h`, 100); got == "" {
		t.Fatal("Sanitize 不该返回空")
	}
	// 长了要截断，且不能把 UTF-8 截成半个字
	long := ""
	for i := 0; i < 100; i++ {
		long += "国"
	}
	if got := Sanitize(long, 30); len(got) > 30 {
		t.Fatalf("截断后还是太长: %d", len(got))
	}

	exts := ParseExts("mp4, .MKV ;ts，m2ts")
	for _, want := range []string{".mp4", ".mkv", ".ts", ".m2ts"} {
		if !exts[want] {
			t.Fatalf("ParseExts 漏了 %s: %v", want, exts)
		}
	}
	if !MatchExt("影片.MKV", exts, false) {
		t.Fatal("扩展名匹配应该忽略大小写")
	}
	if MatchExt("影片.nfo", exts, false) {
		t.Fatal("nfo 不该匹配视频扩展名")
	}
	if !MatchExt("任意.bin", exts, true) {
		t.Fatal("all=true 应该全通过")
	}
	if !IsMeta("影片-poster.jpg") || !IsMeta("影片.nfo") || IsMeta("影片.mkv") {
		t.Fatal("IsMeta 判断不对")
	}
}

func TestRemoveKillsMetaSiblings(t *testing.T) {
	s := newStore(t)
	rel := "电影/影片.strm"
	abs, _, err := s.Write(rel, "http://x/p115/play/pc/影片.mkv/1")
	if err != nil {
		t.Fatalf("Write: %v", err)
	}
	dir := filepath.Dir(abs)
	for _, name := range []string{"影片.nfo", "影片.srt", "影片-poster.jpg"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o644); err != nil {
			t.Fatalf("准备元数据失败: %v", err)
		}
	}

	removed, err := s.RemoveAbs(abs, true)
	if err != nil {
		t.Fatalf("RemoveAbs 报错: %v", err)
	}
	if len(removed) == 0 {
		t.Fatal("RemoveAbs 应该删掉 strm")
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		// 目录被清空后顺手删掉了，也算干净
		return
	}
	for _, e := range entries {
		if !e.IsDir() {
			t.Fatalf("连带删除没清干净，还剩 %s", e.Name())
		}
	}
}
