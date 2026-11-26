package main

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

// 期望值来自参考实现（Python 版 qr_svg_rows）对同样输入算出的矩阵，
// 用哈希比对：只要有一个模块位置不同就会对不上。
func TestQRMatrixMatchesReference(t *testing.T) {
	cases := []struct {
		content string
		size    int
		sum     string
	}{
		{
			"https://open.e.189.cn/api/account/qrClinentLogin.do?paras=new_uuid=0123456789abcdef0123456789abcdef&clientId=8013418323",
			45,
			"0feb0000e6a59e2db4ad81f9edca6a4d49aa9d6c0af6156765d698b9548a596b",
		},
		{
			"https://cloud.189.cn/api/portal/loginUrl.action?redirectURL=https://cloud.189.cn&uuid=abc123",
			41,
			"44e9c55bf8c29dcfcca5a9a33ab2b31e3e339d06b4e369efae57c545a831b4ac",
		},
	}
	for _, tc := range cases {
		rows := qrRows(tc.content)
		if len(rows) != tc.size {
			t.Fatalf("尺寸 %d，期望 %d（内容 %s）", len(rows), tc.size, tc.content[:32])
		}
		sum := sha256.Sum256([]byte(strings.Join(rows, "\n")))
		if got := hex.EncodeToString(sum[:]); got != tc.sum {
			t.Fatalf("矩阵与参考实现不一致：got=%s want=%s", got, tc.sum)
		}
	}
}

// SVG 至少得是合法的 svg 片段，且模块数量对得上。
func TestQRSVG(t *testing.T) {
	svg := qrSVG("https://cloud.189.cn/api/portal/loginUrl.action?uuid=abc")
	if !strings.HasPrefix(svg, "<svg") || !strings.HasSuffix(svg, "</svg>") {
		t.Fatalf("不是完整 SVG：%s", svg[:40])
	}
	if n := strings.Count(svg, "M"); n < 100 {
		t.Fatalf("黑点太少（%d），二维码大概是空的", n)
	}
	if qrSVG(strings.Repeat("x", 20000)) != "" {
		t.Fatal("内容过长时应该返回空串而不是坏图")
	}
}
