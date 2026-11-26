// Package m115 实现 115 客户端（vox）那套请求加密。
//
// 2026-09 之后，115 把「分享直链」「离线任务」这些接口从网页版迁到了 app 通道，
// 请求体要用 m115 编码：随机 16 字节密钥 + XOR 混淆 + RSA 分段加密，响应再用同一把
// 密钥解回来。网页 Cookie 仍然有效，缺的只是这层包装。
//
// 算法与常量移植自 SheltonZhu/115driver（pkg/crypto/m115，MIT License），
// 只做「编码请求 / 解码响应」，不含业务逻辑。
package m115

import (
	"crypto/rand"
	"encoding/base64"
	"io"
)

// Key 是一次请求用的 16 字节临时密钥。
type Key [16]byte

// GenerateKey 生成随机密钥。
func GenerateKey() Key {
	key := Key{}
	_, _ = io.ReadFull(rand.Reader, key[:])
	return key
}

// Encode 把明文编码成 base64(RSA(...))，可直接放进表单的 data 字段。
func Encode(input []byte, key Key) string {
	buf := make([]byte, 16+len(input))
	copy(buf, key[:])
	copy(buf[16:], input)
	xorTransform(buf[16:], xorDeriveKey(key[:], 4))
	reverseBytes(buf[16:])
	xorTransform(buf[16:], xorClientKey)
	return base64.StdEncoding.EncodeToString(rsaEncrypt(buf))
}

// Decode 解回响应里的 data 字段。
func Decode(input string, key Key) ([]byte, error) {
	data, err := base64.StdEncoding.DecodeString(input)
	if err != nil {
		return nil, err
	}
	data = rsaDecrypt(data)
	output := make([]byte, len(data)-16)
	copy(output, data[16:])
	xorTransform(output, xorDeriveKey(data[:16], 12))
	reverseBytes(output)
	xorTransform(output, xorDeriveKey(key[:], 4))
	return output, nil
}
