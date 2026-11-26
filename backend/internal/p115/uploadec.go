package p115

// 115 上传通道（uplb.115.com/4.0/initupload.php）的载荷编解码。
//
// 2026 版的这个接口不再接受明文表单：
//   - 表单参数 urlencode 后 AES-128-CBC 加密，直接当 body 发；
//   - k_ec 放 query，里面塞客户端公钥与时间戳；
//   - 响应同样 AES-CBC 解密，再 LZ4 分帧解压。
//
// 关键点（踩过坑）：这里的「ECDH」其实是**固定密钥对**——客户端私钥写死在
// 客户端里，所以共享密钥也是常量，AES key/iv 直接就是 p115cipher 里的
// AES_KEY / AES_IV，k_ec 里放的也必须是配套的那个固定公钥。
// 自己现生成一对 P-224 密钥看着更「正确」，但 115 用固定私钥算出来的共享
// 密钥跟它对不上，服务端解出来是乱码，只会回一句看不懂的东西。
//
// 参考 ChenyangGao/p115client 的 p115cipher（仍在维护，实现能跑通）。

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"net/url"
)

const (
	// p224BaseLen 是 P-224 单坐标的字节数。
	p224BaseLen = 28
	// uploadCRCSalt 用于 k_ec 的校验和。
	uploadCRCSalt = "^j>WD3Kr?J2gLFjD4W2y@"
)

var (
	// uploadPubKey 是客户端（固定）公钥：1 字节长度 + 1 字节 Y 奇偶 + X。
	// 115 服务端据此算出与本客户端一致的共享密钥。
	uploadPubKey = []byte{
		0x1d, 0x03,
		0x0e, 0x80, 0xA1, 0x78, 0xDC, 0xEE, 0xCE, 0xCD, 0xA3, 0x77, 0xDE, 0x12,
		0x8D, 0x8E, 0xD9, 0xDD, 0xCF, 0x55, 0xAE, 0x61, 0xED, 0x46, 0xEA, 0x12,
		0x1A, 0x1C, 0xFC, 0x81,
	}
	// uploadAESKey / uploadAESIV 是与上面公钥配套的共享密钥前后 16 字节。
	uploadAESKey = []byte{
		0xFB, 0x1A, 0x19, 0xD6, 0x52, 0xF5, 0xAA, 0xF7,
		0xBC, 0x65, 0x1D, 0x0F, 0x69, 0xBF, 0x42, 0x2F,
	}
	uploadAESIV = []byte{
		0x69, 0xBF, 0x42, 0x2F, 0x49, 0x96, 0x05, 0x50,
		0xA0, 0xAD, 0x44, 0xEC, 0x34, 0x46, 0xCB, 0x4C,
	}
)

// uploadCipher 是上传通道的加解密器（密钥固定，无需按次生成）。
type uploadCipher struct {
	key    []byte
	iv     []byte
	pubKey []byte
}

// newUploadCipher 返回固定密钥的加解密器。
func newUploadCipher() (*uploadCipher, error) {
	if len(uploadPubKey) != p224BaseLen+2 {
		return nil, fmt.Errorf("客户端公钥长度不对（%d）", len(uploadPubKey))
	}
	return &uploadCipher{key: uploadAESKey, iv: uploadAESIV, pubKey: uploadPubKey}, nil
}

// encodeToken 把公钥与时间戳打包成 k_ec。
//
//	token = pub[:15] + 00 73 00 00 00 + ts(4 字节小端) + pub[15:]
//	      + 00 01 00 00 00 + crc32(salt+前面全部)(4 字节小端)
//
// 时间戳单位是**秒**（和表单里的 t 必须一致）。
func (c *uploadCipher) encodeToken(ts int64) string {
	token := make([]byte, 0, 48)
	token = append(token, c.pubKey[:15]...)
	token = append(token, 0x00, 0x73, 0x00, 0x00, 0x00)

	var t [4]byte
	binary.LittleEndian.PutUint32(t[:], uint32(ts))
	token = append(token, t[:]...)
	token = append(token, c.pubKey[15:]...)
	token = append(token, 0x00, 0x01, 0x00, 0x00, 0x00)

	sum := crc32.ChecksumIEEE(append(append([]byte(uploadCRCSalt), token...)))
	var crc [4]byte
	binary.LittleEndian.PutUint32(crc[:], sum)
	token = append(token, crc[:]...)

	return base64.StdEncoding.EncodeToString(token)
}

// encrypt 用固定 key/iv 做 AES-CBC。长度已对齐时**不补位**（跟官方实现一致）。
func (c *uploadCipher) encrypt(plain []byte) ([]byte, error) {
	block, err := aes.NewCipher(c.key)
	if err != nil {
		return nil, err
	}
	buf := pkcs7Pad(append([]byte(nil), plain...))
	out := make([]byte, len(buf))
	cipher.NewCBCEncrypter(block, c.iv).CryptBlocks(out, buf)
	return out, nil
}

// decrypt 先按 16 字节截断（响应偶尔带尾巴），再 AES-CBC 解，
// 然后去掉 PKCS#7 补位，最后 LZ4 分帧解压。
func (c *uploadCipher) decrypt(raw []byte) ([]byte, error) {
	if n := len(raw) - len(raw)%aes.BlockSize; n >= aes.BlockSize {
		raw = raw[:n]
	}
	if len(raw) < aes.BlockSize {
		return nil, fmt.Errorf("响应长度不对（%d）", len(raw))
	}
	block, err := aes.NewCipher(c.key)
	if err != nil {
		return nil, err
	}
	plain := make([]byte, len(raw))
	cipher.NewCBCDecrypter(block, c.iv).CryptBlocks(plain, raw)
	plain = pkcs7Unpad(plain)

	// 响应是 LZ4 分帧：[2 字节小端长度][block]…
	return lz4FramesDecompress(plain)
}

// pkcs7Pad 按 16 字节对齐补位（正好对齐时不补，跟官方实现一致）。
func pkcs7Pad(data []byte) []byte {
	if pad := len(data) % aes.BlockSize; pad != 0 {
		pad = aes.BlockSize - pad
		out := make([]byte, 0, len(data)+pad)
		out = append(out, data...)
		for i := 0; i < pad; i++ {
			out = append(out, byte(pad))
		}
		return out
	}
	return data
}

// pkcs7Unpad 宽松去补位：只有末字节 1..15 且整段都是该值时才裁掉。
// （末字节 16 表示整块都是补位，官方实现不裁，这里保持一致。）
func pkcs7Unpad(data []byte) []byte {
	if len(data) == 0 {
		return data
	}
	n := int(data[len(data)-1])
	if n <= 0 || n >= aes.BlockSize || n > len(data) {
		return data
	}
	for _, b := range data[len(data)-n:] {
		if int(b) != n {
			return data
		}
	}
	return data[:len(data)-n]
}

// encodeUploadPayload 把参数表变成「丢空值 → 排序 → urlencode → AES」的密文。
// 官方是 urlencode(sorted((k, v) for k, v in payload.items() if v))：
// 空字符串参数不会出现在请求里，塞进去反而会被判参数非法。
func encodeUploadPayload(c *uploadCipher, payload map[string]string) ([]byte, error) {
	vals := url.Values{}
	for k, v := range payload {
		if v == "" {
			continue
		}
		vals.Set(k, v)
	}
	return c.encrypt([]byte(vals.Encode()))
}

// lz4FramesDecompress 解 115 的 LZ4 分帧：[2 字节小端长度][block]...
func lz4FramesDecompress(src []byte) ([]byte, error) {
	var out []byte
	for len(src) > 2 {
		n := int(binary.LittleEndian.Uint16(src[:2]))
		if n == 0 {
			break
		}
		if len(src) < n+2 {
			return nil, errors.New("LZ4 帧长度越界")
		}
		block, err := lz4BlockDecompress(src[2 : n+2])
		if err != nil {
			return nil, err
		}
		out = append(out, block...)
		src = src[n+2:]
	}
	if out == nil {
		return nil, errors.New("LZ4 数据为空")
	}
	return out, nil
}

// lz4BlockDecompress 解一个标准 LZ4 block。
func lz4BlockDecompress(src []byte) ([]byte, error) {
	out := make([]byte, 0, len(src)*4)
	var i int
	for i < len(src) {
		token := src[i]
		i++
		litLen := int(token >> 4)
		if litLen == 15 {
			for i < len(src) {
				b := int(src[i])
				i++
				litLen += b
				if b != 255 {
					break
				}
			}
		}
		if i+litLen > len(src) {
			return nil, errors.New("LZ4 字面量越界")
		}
		out = append(out, src[i:i+litLen]...)
		i += litLen
		if i >= len(src) {
			break
		}
		if i+2 > len(src) {
			return nil, errors.New("LZ4 偏移截断")
		}
		offset := int(binary.LittleEndian.Uint16(src[i : i+2]))
		i += 2
		if offset == 0 || offset > len(out) {
			return nil, errors.New("LZ4 偏移非法")
		}
		matchLen := int(token & 0x0f)
		if matchLen == 15 {
			for i < len(src) {
				b := int(src[i])
				i++
				matchLen += b
				if b != 255 {
					break
				}
			}
		}
		matchLen += 4
		start := len(out) - offset
		for j := 0; j < matchLen; j++ {
			out = append(out, out[start+j])
		}
	}
	return out, nil
}
