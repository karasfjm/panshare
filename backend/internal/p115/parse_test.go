package p115

import "testing"

// 用户粘的链接形态很多，解析错了就会拿错分享码去转存别人的东西。
func TestParseShare(t *testing.T) {
	cases := []struct {
		in      string
		code    string
		receive string
		wantErr bool
	}{
		{in: "https://115.com/s/swabc1234?password=abcd", code: "swabc1234", receive: "abcd"},
		{in: "https://115.com/s/swabc1234", code: "swabc1234"},
		{in: "https://115cdn.com/s/swabc1234#abcd", code: "swabc1234", receive: "abcd"},
		{in: "https://115.com/s/swabc1234-abcd", code: "swabc1234", receive: "abcd"},
		{in: "swabc1234-abcd", code: "swabc1234", receive: "abcd"},
		{in: "swabc1234", code: "swabc1234"},
		{in: "   https://115.com/s/swabc1234/?password=xy12   ", code: "swabc1234", receive: "xy12"},
		{in: "https://115.com/s/swabc1234\n第二行忽略", code: "swabc1234"},
		{in: "", wantErr: true},
		{in: "这不是链接", wantErr: true},
	}
	for _, c := range cases {
		got, err := ParseShare(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("ParseShare(%q) 应该报错，实际 %+v", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseShare(%q) 报错: %v", c.in, err)
			continue
		}
		if got.ShareCode != c.code || got.ReceiveCode != c.receive {
			t.Errorf("ParseShare(%q) = %+v，期望 code=%q receive=%q", c.in, got, c.code, c.receive)
		}
	}
}

func TestParseEd2k(t *testing.T) {
	name, size, hash, err := ParseEd2k("ed2k://|file|Some.Movie.2024.1080p.mkv|1234567890|ABCDEF0123456789ABCDEF0123456789|/")
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if name != "Some.Movie.2024.1080p.mkv" {
		t.Errorf("名字不对: %q", name)
	}
	if size != 1234567890 {
		t.Errorf("大小不对: %d", size)
	}
	if hash != "abcdef0123456789abcdef0123456789" {
		t.Errorf("hash 应统一小写: %q", hash)
	}

	if _, _, _, err := ParseEd2k("magnet:?xt=urn:btih:abc"); err == nil {
		t.Error("非 ed2k 链接应报错")
	}
	if _, _, _, err := ParseEd2k(""); err == nil {
		t.Error("空串应报错")
	}
}
