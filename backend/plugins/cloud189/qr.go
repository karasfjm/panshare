package main

import (
	"fmt"
	"math/bits"
	"strings"
)

// 二维码矩阵：byte 模式 + 固定 mask 0，纯标准库实现（无第三方依赖）。
//
// 移植自参考实现的 core.py（qr_matrix / qr_svg_rows）。之所以自己算而不引 SDK：
// 插件是按单文件二进制分发、跑在容器里的，能少一个依赖就少一处「内网拉不到包」的风险；
// 而且二维码内容（天翼下发的登录 URL）随时可能变长，本地算不用管版本选择。

// rsBlockTable[版本-1][纠错级别] = 若干组 (块数, 总码字, 数据码字)。
// 纠错级别顺序固定为 L / M / Q / H。
var rsBlockTable = [15][4][]int{
	{{1, 26, 19}, {1, 26, 16}, {1, 26, 13}, {1, 26, 9}},
	{{1, 44, 34}, {1, 44, 28}, {1, 44, 22}, {1, 44, 16}},
	{{1, 70, 55}, {1, 70, 44}, {2, 35, 17}, {2, 35, 13}},
	{{1, 100, 80}, {2, 50, 32}, {2, 50, 24}, {4, 25, 9}},
	{{1, 134, 108}, {2, 67, 43}, {2, 33, 15, 2, 34, 16}, {2, 33, 11, 2, 34, 12}},
	{{2, 86, 68}, {4, 43, 27}, {4, 43, 19}, {4, 43, 15}},
	{{2, 98, 78}, {4, 49, 31}, {2, 32, 14, 4, 33, 15}, {4, 39, 13, 1, 40, 14}},
	{{2, 121, 97}, {2, 60, 38, 2, 61, 39}, {4, 40, 18, 2, 41, 19}, {4, 40, 14, 2, 41, 15}},
	{{2, 146, 116}, {3, 58, 36, 2, 59, 37}, {4, 36, 16, 4, 37, 17}, {4, 36, 12, 4, 37, 13}},
	{{2, 86, 68, 2, 87, 69}, {4, 69, 43, 1, 70, 44}, {6, 43, 19, 2, 44, 20}, {6, 43, 15, 2, 44, 16}},
	{{4, 101, 81}, {1, 80, 50, 4, 81, 51}, {4, 50, 22, 4, 51, 23}, {3, 36, 12, 8, 37, 13}},
	{{2, 116, 92, 2, 117, 93}, {6, 58, 36, 2, 59, 37}, {4, 46, 20, 6, 47, 21}, {7, 42, 14, 4, 43, 15}},
	{{4, 133, 107}, {8, 59, 37, 1, 60, 38}, {8, 44, 20, 4, 45, 21}, {12, 33, 11, 4, 34, 12}},
	{{3, 145, 115, 1, 146, 116}, {4, 64, 40, 5, 65, 41}, {11, 36, 16, 5, 37, 17}, {11, 36, 12, 5, 37, 13}},
	{{5, 109, 87, 1, 110, 88}, {5, 65, 41, 5, 66, 42}, {5, 54, 24, 7, 55, 25}, {11, 36, 12, 7, 37, 13}},
}

// alignPos[版本-1]：对齐图案中心点。
var alignPos = [15][]int{
	{}, {6, 18}, {6, 22}, {6, 26}, {6, 30}, {6, 34},
	{6, 22, 38}, {6, 24, 42}, {6, 26, 46}, {6, 28, 50}, {6, 30, 54},
	{6, 34, 62}, {6, 26, 46, 66}, {6, 26, 48, 70},
	{6, 26, 50, 74},
}

var (
	gfExp [512]int
	gfLog [256]int
)

func init() {
	x := 1
	for i := 0; i < 255; i++ {
		gfExp[i] = x
		gfLog[x] = i
		x <<= 1
		if x&0x100 != 0 {
			x ^= 0x11D
		}
	}
	for i := 255; i < 512; i++ {
		gfExp[i] = gfExp[i-255]
	}
}

func gfMul(a, b int) int {
	if a == 0 || b == 0 {
		return 0
	}
	return gfExp[gfLog[a]+gfLog[b]]
}

// rsECC 计算 Reed-Solomon 纠错码字。
func rsECC(data []int, nsym int) []int {
	gen := []int{1}
	for i := 0; i < nsym; i++ {
		factor := gfExp[i]
		nxt := make([]int, len(gen)+1)
		for j, coef := range gen {
			nxt[j] ^= coef
			nxt[j+1] ^= gfMul(coef, factor)
		}
		gen = nxt
	}
	res := make([]int, len(data)+nsym)
	copy(res, data)
	for i := 0; i < len(data); i++ {
		coef := res[i]
		if coef == 0 {
			continue
		}
		for j, g := range gen {
			res[i+j] ^= gfMul(g, coef)
		}
	}
	return res[len(data):]
}

func bchTypeInfo(data int) int {
	d := data << 10
	for d > 0 && bits.Len(uint(d))-11 >= 0 {
		d ^= 0x537 << (bits.Len(uint(d)) - 11)
	}
	return ((data << 10) | d) ^ 0x5412
}

func bchVersionInfo(version int) int {
	d := version << 12
	for d > 0 && bits.Len(uint(d))-13 >= 0 {
		d ^= 0x1F25 << (bits.Len(uint(d)) - 13)
	}
	return (version << 12) | d
}

// ecBlocks 把 rsBlockTable 变成逐块的 (总码字, 数据码字) 列表。
func ecBlocks(version, ecIndex int) [][2]int {
	spec := rsBlockTable[version-1][ecIndex]
	var out [][2]int
	for i := 0; i+2 < len(spec); i += 3 {
		for n := 0; n < spec[i]; n++ {
			out = append(out, [2]int{spec[i+1], spec[i+2]})
		}
	}
	return out
}

// qrMatrix 生成二维码矩阵，dark[row][col] 为 true 表示黑点。
func qrMatrix(text, ec string) ([][]bool, error) {
	payload := []byte(text)
	ecIndex := strings.Index("LMQH", strings.ToUpper(ec))
	if ecIndex < 0 {
		ecIndex = 1 // 默认 M
	}
	ecFormat := [4]int{1, 0, 3, 2}[ecIndex]

	version := 0
	for v := 1; v <= len(rsBlockTable); v++ {
		// 可用容量只算数据码字（纠错码字不承载内容）
		capacity := 0
		for _, b := range ecBlocks(v, ecIndex) {
			capacity += b[1] * 8
		}
		countBits := 8
		if v >= 10 {
			countBits = 16
		}
		if 4+countBits+len(payload)*8 <= capacity {
			version = v
			break
		}
	}
	if version == 0 {
		return nil, fmt.Errorf("二维码内容过长（%d 字节）", len(payload))
	}

	blocks := ecBlocks(version, ecIndex)
	dataCW := 0
	for _, b := range blocks {
		dataCW += b[1]
	}

	var bitBuf []int
	put := func(value, length int) {
		for i := length - 1; i >= 0; i-- {
			bitBuf = append(bitBuf, (value>>i)&1)
		}
	}
	put(0b0100, 4) // byte 模式
	if version < 10 {
		put(len(payload), 8)
	} else {
		put(len(payload), 16)
	}
	for _, b := range payload {
		put(int(b), 8)
	}
	capacityBits := dataCW * 8
	if left := capacityBits - len(bitBuf); left > 0 {
		if left > 4 {
			left = 4
		}
		put(0, left)
	}
	for len(bitBuf)%8 != 0 {
		bitBuf = append(bitBuf, 0)
	}
	codewords := make([]int, 0, len(bitBuf)/8)
	for i := 0; i < len(bitBuf); i += 8 {
		v := 0
		for _, b := range bitBuf[i : i+8] {
			v = v<<1 | b
		}
		codewords = append(codewords, v)
	}
	for pad := 0; len(codewords) < dataCW; pad++ {
		if pad%2 == 0 {
			codewords = append(codewords, 0xEC)
		} else {
			codewords = append(codewords, 0x11)
		}
	}

	var dataBlocks, eccBlocks [][]int
	offset := 0
	for _, b := range blocks {
		total, dcw := b[0], b[1]
		chunk := codewords[offset : offset+dcw]
		offset += dcw
		dataBlocks = append(dataBlocks, chunk)
		eccBlocks = append(eccBlocks, rsECC(chunk, total-dcw))
	}
	maxData := 0
	for _, b := range dataBlocks {
		if len(b) > maxData {
			maxData = len(b)
		}
	}
	maxECC := 0
	for _, b := range eccBlocks {
		if len(b) > maxECC {
			maxECC = len(b)
		}
	}
	var interleaved []int
	for i := 0; i < maxData; i++ {
		for _, b := range dataBlocks {
			if i < len(b) {
				interleaved = append(interleaved, b[i])
			}
		}
	}
	for i := 0; i < maxECC; i++ {
		for _, b := range eccBlocks {
			if i < len(b) {
				interleaved = append(interleaved, b[i])
			}
		}
	}

	size := version*4 + 17
	matrix := make([][]bool, size)
	fixed := make([][]bool, size)
	for i := range matrix {
		matrix[i] = make([]bool, size)
		fixed[i] = make([]bool, size)
	}
	setModule := func(row, col int, dark bool) {
		if row >= 0 && row < size && col >= 0 && col < size {
			matrix[row][col] = dark
			fixed[row][col] = true
		}
	}

	drawFinder := func(top, left int) {
		for r := -1; r < 8; r++ {
			for c := -1; c < 8; c++ {
				inside := r >= 0 && r <= 6 && c >= 0 && c <= 6
				dark := inside && (r == 0 || r == 6 || c == 0 || c == 6 ||
					(r >= 2 && r <= 4 && c >= 2 && c <= 4))
				setModule(top+r, left+c, dark)
			}
		}
	}
	drawFinder(0, 0)
	drawFinder(0, size-7)
	drawFinder(size-7, 0)

	for i := 8; i < size-8; i++ {
		dark := i%2 == 0
		setModule(6, i, dark)
		setModule(i, 6, dark)
	}

	for _, r := range alignPos[version-1] {
		for _, c := range alignPos[version-1] {
			if (r == 6 && c == 6) || (r == 6 && c == size-7) || (r == size-7 && c == 6) {
				continue
			}
			for dr := -2; dr <= 2; dr++ {
				for dc := -2; dc <= 2; dc++ {
					dark := max(abs(dr), abs(dc)) != 1
					setModule(r+dr, c+dc, dark)
				}
			}
		}
	}

	for i := 0; i < 9; i++ {
		if i != 6 {
			setModule(8, i, false)
			setModule(i, 8, false)
		}
	}
	for i := 0; i < 8; i++ {
		setModule(8, size-1-i, false)
		setModule(size-1-i, 8, false)
	}
	setModule(size-8, 8, true) // dark module

	if version >= 7 {
		vbits := bchVersionInfo(version)
		for i := 0; i < 18; i++ {
			dark := (vbits>>i)&1 == 1
			setModule(size-11+i%3, i/3, dark)
			setModule(i/3, size-11+i%3, dark)
		}
	}

	// 固定 mask 0：(row+col) 为偶数时翻转
	maskBit := func(r, c int) bool { return (r+c)%2 == 0 }
	idx := 0
	totalBits := len(interleaved) * 8
	upward := true
	for col := size - 1; col > 0; col -= 2 {
		if col == 6 {
			col--
		}
		for k := 0; k < size; k++ {
			row := k
			if upward {
				row = size - 1 - k
			}
			for _, c := range []int{col, col - 1} {
				if fixed[row][c] {
					continue
				}
				dark := false
				if idx < totalBits {
					dark = (interleaved[idx/8]>>(7-idx%8))&1 == 1
					idx++
				}
				matrix[row][c] = dark != maskBit(row, c)
			}
		}
		upward = !upward
	}

	fmtBits := bchTypeInfo(ecFormat << 3)
	for i := 0; i < 15; i++ {
		dark := (fmtBits>>i)&1 == 1
		switch {
		case i < 6:
			matrix[i][8] = dark
		case i < 8:
			matrix[i+1][8] = dark
		default:
			matrix[size-15+i][8] = dark
		}
		switch {
		case i < 8:
			matrix[8][size-i-1] = dark
		case i < 9:
			matrix[8][15-i] = dark
		default:
			matrix[8][14-i] = dark
		}
	}
	return matrix, nil
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

// qrRows 把矩阵转成 "0101…" 的字符串行，方便前端或测试直接比对。
func qrRows(text string) []string {
	m, err := qrMatrix(text, "M")
	if err != nil {
		return nil
	}
	rows := make([]string, len(m))
	for r, row := range m {
		var b strings.Builder
		b.Grow(len(row))
		for _, dark := range row {
			if dark {
				b.WriteByte('1')
			} else {
				b.WriteByte('0')
			}
		}
		rows[r] = b.String()
	}
	return rows
}

// qrSVG 直接吐一段 SVG，页面里 setInnerHTML 即可，不依赖任何外部二维码服务。
func qrSVG(text string) string {
	m, err := qrMatrix(text, "M")
	if err != nil {
		return ""
	}
	size := len(m)
	var b strings.Builder
	b.Grow(size*size + 256)
	fmt.Fprintf(&b, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" `+
		`width="220" height="220" shape-rendering="crispEdges">`, size+2, size+2)
	b.WriteString(`<rect width="100%" height="100%" fill="#ffffff"/>`)
	b.WriteString(`<path fill="#000000" d="`)
	for r := 0; r < size; r++ {
		for c := 0; c < size; c++ {
			if m[r][c] {
				fmt.Fprintf(&b, "M%d %dh1v1h-1z", c+1, r+1)
			}
		}
	}
	b.WriteString(`"/></svg>`)
	return b.String()
}
