package p115

import (
	"context"
	"os"
	"testing"
	"time"
)

// TestCloudDownLive 打真实接口，验证 app 通道的 m115 编码能被 115 正确解开。
//
// 需要登录态：P115_COOKIES="UID=...; CID=..."，否则用一个无效 Cookie 跑，
// 只要能拿到「请重新登录」（而不是 ssp 的 decode fail），就说明请求体没问题。
//
//	P115_LIVE=1 P115_COOKIES="..." go test ./internal/p115 -run TestCloudDownLive -v
func TestCloudDownLive(t *testing.T) {
	if os.Getenv("P115_LIVE") == "" {
		t.Skip("设置 P115_LIVE=1 才跑真实接口")
	}
	c := New(os.Getenv("P115_COOKIES"))
	if c.Cookies == "" {
		c.Cookies = "UID=0_A1_1; CID=0000000000000000000"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	link := os.Getenv("P115_ED2K")
	if link == "" {
		link = "ed2k://|file|ubuntu-24.04.1-desktop-amd64.iso|6114590720|A1B2C3D4E5F60718293A4B5C6D7E8F90|/"
	}
	m, err := c.cloudDownCall(ctx, URLCloudDownAdd, "add_task_url", map[string]string{
		"url":        link,
		"wp_path_id": os.Getenv("P115_CID"),
	})
	if err != nil {
		t.Fatalf("提交请求失败：%v", err)
	}
	t.Logf("add_task_url → %v", m)

	m2, err := c.OfflineList(ctx)
	if err != nil {
		t.Logf("task_lists → 错误（预期：未登录）%v", err)
	} else {
		t.Logf("task_lists → %d 条", len(m2))
	}

	m3, err := c.OfflineTaskInfo(ctx, "a1b2c3d4e5f60718293a4b5c6d7e8f90")
	if err != nil {
		t.Logf("get_user_task → 错误（预期：未登录）%v", err)
	} else {
		t.Logf("get_user_task → %v", m3)
	}
}
