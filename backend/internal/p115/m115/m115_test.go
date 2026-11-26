package m115

import (
	"encoding/base64"
	"strings"
	"testing"
)

// Encode 的输出会被 115 按 128 字节一段做 RSA 解密，长度必须是 128 的整数倍。
func TestEncodeBlocks(t *testing.T) {
	key := GenerateKey()
	for _, input := range []string{`{"share_code":"swabc","file_id":"1"}`, ""} {
		out := Encode([]byte(input), key)
		raw, err := base64.StdEncoding.DecodeString(out)
		if err != nil {
			t.Fatalf("Encode 输出不是合法 base64：%v", err)
		}
		if len(raw) == 0 || len(raw)%128 != 0 {
			t.Fatalf("密文长度 %d，应为 128 的整数倍（输入 %q）", len(raw), input)
		}
	}
}

// 同一密钥、同一明文，两次编码结果必须不同（里面有随机填充），但长度一致。
func TestEncodeNonDeterministic(t *testing.T) {
	key := GenerateKey()
	a := Encode([]byte("hello"), key)
	b := Encode([]byte("hello"), key)
	if a == b {
		t.Fatal("两次编码结果相同，说明随机填充没生效")
	}
	if len(a) != len(b) {
		t.Fatalf("长度不一致：%d vs %d", len(a), len(b))
	}
}

func TestDecodeRejectsGarbage(t *testing.T) {
	if _, err := Decode("not-base64!!", GenerateKey()); err == nil {
		t.Fatal("非法 base64 应该报错")
	}
}

func TestXorDeriveKeyStable(t *testing.T) {
	seed := make([]byte, 16)
	k1 := xorDeriveKey(seed, 4)
	k2 := xorDeriveKey(seed, 4)
	if len(k1) != 4 {
		t.Fatalf("派生密钥长度 %d，应为 4", len(k1))
	}
	for i := range k1 {
		if k1[i] != k2[i] {
			t.Fatal("同一种子两次派生结果不一致")
		}
	}
	if strings.Repeat("\x00", 4) == string(k1) {
		t.Fatal("派生密钥全零，混淆表加载有误")
	}
}
