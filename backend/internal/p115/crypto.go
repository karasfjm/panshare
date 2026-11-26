package p115

// 115 的「加密通道」协议。
//
// 背景：/files/download 的 GET 方式只允许 200MB 以内的文件，视频和大文件会
// 直接返回 50028（文件大小超出限制，请使用115电脑端下载）。要拿到这些文件
// 的直链，必须走 POST + 加密参数：
//
//	POST https://webapi.115.com/files/download   data=<rsa 密文>
//	POST https://proapi.115.com/app/chrome/downurl data=<rsa 密文>
//
// 两边都是同一套：把 JSON 做一次 XOR 混淆，再用 1024 位 RSA（这里被当成
// 「客户端与服务端共用同一对 n/e」的对称变换用：加密 pow(m,e,n)，解密也是
// pow(c,e,n)）加密后 base64。响应里的 data 同样用它换回明文 JSON。
//
// 这一段与 p115client / p115cipher 的实现等价，改之前请先看那里的注释。

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"strings"
)

// Gkts 是密钥派生用的固定表。
var gkts = []byte{
	0xf0, 0xe5, 0x69, 0xae, 0xbf, 0xdc, 0xbf, 0x8a, 0x1a, 0x45, 0xe8, 0xbe, 0x7d, 0xa6, 0x73, 0xb8,
	0xde, 0x8f, 0xe7, 0xc4, 0x45, 0xda, 0x86, 0xc4, 0x9b, 0x64, 0x8b, 0x14, 0x6a, 0xb4, 0xf1, 0xaa,
	0x38, 0x01, 0x35, 0x9e, 0x26, 0x69, 0x2c, 0x86, 0x00, 0x6b, 0x4f, 0xa5, 0x36, 0x34, 0x62, 0xa6,
	0x2a, 0x96, 0x68, 0x18, 0xf2, 0x4a, 0xfd, 0xbd, 0x6b, 0x97, 0x8f, 0x4d, 0x8f, 0x89, 0x13, 0xb7,
	0x6c, 0x8e, 0x93, 0xed, 0x0e, 0x0d, 0x48, 0x3e, 0xd7, 0x2f, 0x88, 0xd8, 0xfe, 0xfe, 0x7e, 0x86,
	0x50, 0x95, 0x4f, 0xd1, 0xeb, 0x83, 0x26, 0x34, 0xdb, 0x66, 0x7b, 0x9c, 0x7e, 0x9d, 0x7a, 0x81,
	0x32, 0xea, 0xb6, 0x33, 0xde, 0x3a, 0xa9, 0x59, 0x34, 0x66, 0x3b, 0xaa, 0xba, 0x81, 0x60, 0x48,
	0xb9, 0xd5, 0x81, 0x9c, 0xf8, 0x6c, 0x84, 0x77, 0xff, 0x54, 0x78, 0x26, 0x5f, 0xbe, 0xe8, 0x1e,
	0x36, 0x9f, 0x34, 0x80, 0x5c, 0x45, 0x2c, 0x9b, 0x76, 0xd5, 0x1b, 0x8f, 0xcc, 0xc3, 0xb8, 0xf5,
}

var (
	// XOR 环节用的两把固定 key。
	rsaKey = []byte{0x8d, 0xa5, 0xa5, 0x8d}
	gKeyL  = []byte{0x78, 0x06, 0xad, 0x4c, 0x33, 0x86, 0x5d, 0x18, 0x4c, 0x01, 0x3f, 0x46}

	rsaN, _ = new(big.Int).SetString("8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021"+
		"36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad6338c3bb6ac6227502d010993ac"+
		"967d1aef00f0c8e038de2e4d3bc2ec368af2e9f10a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c"+
		"4fe57df3afb71683", 16)
	rsaE = big.NewInt(65537)

	rsaBlock = 128
)

// xorChunked 对应 p115cipher 里那个分段 xor，注意它**不是**简单的按 key 取模：
//
//   - 先处理 len(src) % 4 个头字节（与 key 从头对齐异或）；
//   - 之后每 len(key) 字节一块，每块都从 key 的第 0 个字节重新对齐。
//
// 写成取模看着一样，实际从第 4 个字节起就全错了 —— 直链解出来全是乱码。
func xorChunked(src, key []byte) []byte {
	if len(key) == 0 {
		return append([]byte(nil), src...)
	}
	out := make([]byte, len(src))
	head := len(src) & 3
	for k := 0; k < head && k < len(key) && k < len(src); k++ {
		out[k] = src[k] ^ key[k]
	}
	for pos := head; pos < len(src); {
		n := len(key)
		if pos+n > len(src) {
			n = len(src) - pos
		}
		for k := 0; k < n; k++ {
			out[pos+k] = src[pos+k] ^ key[k]
		}
		pos += n
	}
	return out
}

// deriveKey 是那个 sk_len 字节的派生函数（解密时用响应里的 16 字节 rand key）。
func deriveKey(randKey []byte, skLen int) []byte {
	out := make([]byte, skLen)
	length := skLen * (skLen - 1)
	index := 0
	for i := 0; i < skLen; i++ {
		x := (randKey[i] + gkts[index]) & 0xff
		out[i] = gkts[length] ^ x
		length -= skLen
		index += skLen
	}
	return out
}

// rsaPow 对一块数据做 pow(x, e, n) 并按 block 长度左补零。
func rsaPow(in []byte) []byte {
	m := new(big.Int).SetBytes(in)
	c := new(big.Int).Exp(m, rsaE, rsaN)
	b := c.Bytes()
	if len(b) >= rsaBlock {
		return b
	}
	out := make([]byte, rsaBlock)
	copy(out[rsaBlock-len(b):], b)
	return out
}

// encryptPayload 把任意 JSON 明文变成 115 要的 data 参数。
func encryptPayload(plain []byte) string {
	// 前 16 字节是 rand key，这里固定全 0（与官方客户端一致）
	body := make([]byte, 0, len(plain)+16)
	tmp := xorChunked(plain, rsaKey)
	reverse(tmp)
	body = append(body, make([]byte, 16)...)
	body = append(body, xorChunked(tmp, gKeyL)...)

	var cipher []byte
	for i := 0; i < len(body); i += rsaBlock - 11 {
		end := i + rsaBlock - 11
		if end > len(body) {
			end = len(body)
		}
		chunk := body[i:end]
		// PKCS#1 v1.5 填充：0x00 || 0x02*... || 0x00 || 明文
		pad := make([]byte, rsaBlock)
		pad[1] = 0x02
		for k := 2; k < rsaBlock-1-len(chunk); k++ {
			pad[k] = 0x02
		}
		pad[rsaBlock-1-len(chunk)] = 0
		copy(pad[rsaBlock-len(chunk):], chunk)
		pad[0] = 0
		cipher = append(cipher, rsaPow(pad)...)
	}
	return base64.StdEncoding.EncodeToString(cipher)
}

// decryptPayload 还原响应里的 data 字段。
func decryptPayload(cipherB64 string) ([]byte, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(cipherB64))
	if err != nil {
		// 个别响应是 URL-safe base64
		raw, err = base64.RawURLEncoding.DecodeString(strings.TrimSpace(cipherB64))
		if err != nil {
			return nil, fmt.Errorf("密文解码失败: %w", err)
		}
	}
	var plain []byte
	for i := 0; i+rsaBlock <= len(raw); i += rsaBlock {
		// 解密结果要按「最短字节表示」处理：填充段开头的 0x00 在整数里不存在，
		// 剩下 0x02…0x02 0x00 明文，跳过第一个 0x00 就是正文。
		b := new(big.Int).Exp(new(big.Int).SetBytes(raw[i:i+rsaBlock]), rsaE, rsaN).Bytes()
		idx := 0
		for idx < len(b) && b[idx] != 0 {
			idx++
		}
		if idx >= len(b) {
			return nil, fmt.Errorf("密文分块里没有分隔符")
		}
		plain = append(plain, b[idx+1:]...)
	}
	if len(plain) < 16 {
		return nil, fmt.Errorf("明文太短")
	}
	key := deriveKey(plain[:16], 12)
	body := xorChunked(plain[16:], key)
	reverse(body)
	return xorChunked(body, rsaKey), nil
}

// decryptJSON 解密并解析成 map，给下载接口用。
func decryptJSON(cipherB64 string) (map[string]any, error) {
	b, err := decryptPayload(cipherB64)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("明文不是 JSON: %w", err)
	}
	return m, nil
}

func reverse(b []byte) {
	for i, j := 0, len(b)-1; i < j; i, j = i+1, j-1 {
		b[i], b[j] = b[j], b[i]
	}
}
