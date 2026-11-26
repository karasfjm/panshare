// 插件自带的轻量 KV 存储：仿主库 meta 表的 GetMeta/SetMeta 接口，
// 数据落到 <data-dir>/wxip/meta.json，独立进程不依赖主服务的 SQLite。
package core

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
)

type KV struct {
	mu   sync.Mutex
	file string
	data map[string]string
}

func NewKV(dir string) *KV {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		// 目录建不出来后面写文件也会报错，这里不吞掉：交给调用方看到
		return &KV{file: filepath.Join(dir, "meta.json"), data: map[string]string{}}
	}
	k := &KV{file: filepath.Join(dir, "meta.json"), data: map[string]string{}}
	if raw, err := os.ReadFile(k.file); err == nil {
		_ = json.Unmarshal(raw, &k.data)
	}
	if k.data == nil {
		k.data = map[string]string{}
	}
	return k
}

func (k *KV) GetMeta(key, def string) string {
	k.mu.Lock()
	defer k.mu.Unlock()
	if v, ok := k.data[key]; ok {
		return v
	}
	return def
}

func (k *KV) SetMeta(key, value string) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.data[key] = value
	raw, err := json.MarshalIndent(k.data, "", "  ")
	if err != nil {
		return err
	}
	tmp := k.file + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, k.file)
}
