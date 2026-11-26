package main

import "testing"

// 新版接口把「登录跳转 URL」当 uuid 下发，二维码里必须原样编码；
// 再套 loginUrl.action 模板会拼出坏内容（用户扫码后进不去）。
func TestQRContentUsesRawURL(t *testing.T) {
	raw := "https://open.e.189.cn/api/account/qrClinentLogin.do?paras=new_uuid%3Dabc%7C123"
	if got := qrContent(raw); got != raw {
		t.Fatalf("http 形态应原样返回\n got: %s\nwant: %s", got, raw)
	}
	plain := "0123456789abcdef"
	want := "https://cloud.189.cn/api/portal/loginUrl.action?redirectURL=https://cloud.189.cn&uuid=" + plain
	if got := qrContent(plain); got != want {
		t.Fatalf("裸 uuid 要套模板\n got: %s\nwant: %s", got, want)
	}
}

func TestQRPollUUIDExtractsNewUUID(t *testing.T) {
	raw := "https://open.e.189.cn/api/account/qrClinentLogin.do?paras=new_uuid%3Do0hzuu9v6uehzrba%7C8013418323"
	want := "o0hzuu9v6uehzrba|8013418323"
	if got := qrPollUUID(raw); got != want {
		t.Fatalf("没从 paras 里拆出 uuid\n got: %s\nwant: %s", got, want)
	}
	// 老口径：本来就是裸 uuid，别动它
	if got := qrPollUUID(want); got != want {
		t.Fatalf("裸 uuid 不该被改\n got: %s", got)
	}
	// 没有 paras 的 URL 只能原样回传，让上层再兜底试
	odd := "https://open.e.189.cn/api/account/qrClinentLogin.do"
	if got := qrPollUUID(odd); got != odd {
		t.Fatalf("没有 paras 时应原样返回\n got: %s", got)
	}
}
