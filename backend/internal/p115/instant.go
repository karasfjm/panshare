package p115

import (
	"bytes"
	"context"
	"crypto/md5"
	"crypto/sha1"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// ErrInstantNoContent 表示 115 的资源库里没有这份内容（status=1，只能真上传）。
// 这种失败换凭据重试没有意义，调用方应当直接放弃。
var ErrInstantNoContent = errors.New("115 上没有这份文件的内容，无法秒传")

// 115 秒传（跨账号）。
//
// 走的还是 uplb.115.com/4.0/initupload.php，但 2026 版要求明文表单里带上
// sig 与 token：两者都是「sha1 / md5 加盐」的固定算法，不涉及加密，
// 所以这里直接自算，不用 ECDH。
//
// 流程：
//  1. GET proapi.115.com/android/2.0/user/upload_key 拿 userkey
//  2. POST initupload.php（plain），status=2 就是秒传成功
//  3. status=7 表示要二次校验：拿 sign_check 区间去源文件读一段，
//     sign_val = sha1(那段数据).upper()，带上 sign_key 再提交一次
const (
	// uploadMD5Salt 是 token 的盐，来自官方客户端。
	uploadMD5Salt = "Qclm8MGWUv59TnrR0XPg"
	// uploadAppVer 参与 token 计算，必须与真实客户端一致；36.2.28 是官方安卓端在用的值。
	uploadAppVer = "36.2.28"
	// uploadUA 上传通道只认 115 客户端 UA。
	uploadUA = "Mozilla/5.0 115disk/" + uploadAppVer + " 115Browser/" + uploadAppVer +
		" 115wangpan_android/" + uploadAppVer
)

// 秒传初始化返回的几种状态。
const (
	uploadStatusOK      = 1 // 需要真正上传（115 上没有这份内容，秒传不了）
	uploadStatusReuse   = 2 // 秒传成功
	uploadStatusNeedSig = 7 // 需要二次校验
)

// UploadInfo 取上传凭据（endpoint / gettokenurl）。秒传本身用不到，
// 但真正上传的分片流程要用，留着给后续扩展。
func (c *Client) UploadInfo(ctx context.Context) (map[string]string, error) {
	uid := c.UID(ctx)
	q := url.Values{}
	if uid != "" {
		q.Set("userid", uid)
	}
	m, err := c.getJSON(ctx, UploadAPI+pathUploadInfo+"?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	if err := check("获取上传凭据", m); err != nil {
		return nil, err
	}
	d := subMap(m, "data")
	if len(d) == 0 {
		d = m
	}
	out := map[string]string{}
	for k, v := range d {
		if s, ok := v.(string); ok {
			out[k] = s
		}
	}
	if out["userkey"] == "" {
		out["userkey"] = str(m, "userkey")
	}
	return out, nil
}

// UploadKey 取 userkey（秒传签名要用）。缓存到 client 上，避免每次播放都请求。
//
// 注意：userkey 在 webapi 的 /files/uploadinfo 里，不在 proapi 的 upload_key。
// 用错来源算出来的 sig 会被 115 判成「sig invalid」，而且不会有别的提示。
func (c *Client) UploadKey(ctx context.Context) (string, error) {
	c.uploadKeyMu.Lock()
	if c.uploadKey != "" {
		key := c.uploadKey
		c.uploadKeyMu.Unlock()
		return key, nil
	}
	c.uploadKeyMu.Unlock()

	m, err := c.getJSON(ctx, WebAPI+pathUploadUserkey, nil)
	if err != nil {
		return "", fmt.Errorf("取 userkey 失败：%w", err)
	}
	key := str(m, "userkey")
	if key == "" {
		if d := subMap(m, "data"); len(d) > 0 {
			key = str(d, "userkey")
		}
	}
	if key == "" {
		if n, ok := m["state"].(bool); ok && !n {
			return "", fmt.Errorf("取 userkey 失败：115 拒绝（%v）", m["message"])
		}
		return "", fmt.Errorf("取 userkey 失败：115 没返回 userkey")
	}
	c.uploadKeyMu.Lock()
	c.uploadKey = key
	c.uploadKeyMu.Unlock()
	return key, nil
}

// ForgetUploadKey 丢掉缓存的 userkey。
//
// 115 会轮换 userkey，而它拒绝一份轮换过的凭据时**不一定报错**：常常照样
// 回 200，只是把 status 写成「不可秒传」。所以秒传被拒时要先怀疑凭据过期，
// 丢掉缓存换一份再试一次 —— 否则这份坏凭据会被一直用到进程重启。
func (c *Client) ForgetUploadKey() {
	c.uploadKeyMu.Lock()
	c.uploadKey = ""
	c.uploadKeyMu.Unlock()
}

// RangeFetcher 用来在 115 要求二次校验时，从源文件读取指定区间的字节。
// signCheck 形如 "12345-23456"。
type RangeFetcher func(signCheck string) ([]byte, error)

// RapidUpload 秒传一个文件到 targetDirID。
//
// 原理：115 用 sha1 + size 唯一标识文件内容。上传初始化时若服务器上已有
// 相同内容（任何人传过都算），就能直接「秒传」——只传校验块甚至不传。
// 大于 1MB 的文件 115 会要求二次校验，这时用 fetcher 从源直链读一段数据回传。
func (c *Client) RapidUpload(ctx context.Context, targetDirID, name, sha1sum string, size int64, fetcher RangeFetcher) (string, error) {
	pick, err := c.rapidUploadOnce(ctx, targetDirID, name, sha1sum, size, fetcher)
	if err == nil || ctx.Err() != nil || errors.Is(err, ErrInstantNoContent) {
		return pick, err
	}
	// 115 拒绝一份轮换过的 userkey 时常常不报错，只是回「不可秒传」。
	// 所以第一次失败先当凭据过期处理：丢掉缓存换一份，再试一次。
	c.ForgetUploadKey()
	if ctx.Err() != nil {
		return "", err
	}
	if pick, second := c.rapidUploadOnce(ctx, targetDirID, name, sha1sum, size, fetcher); second == nil {
		return pick, nil
	} else {
		return "", second
	}
}

// rapidUploadOnce 是「一次」秒传尝试（用当前缓存的 userkey），返回 pickcode。
func (c *Client) rapidUploadOnce(ctx context.Context, targetDirID, name, sha1sum string, size int64, fetcher RangeFetcher) (string, error) {
	sha1sum = strings.ToUpper(strings.TrimSpace(sha1sum))
	if sha1sum == "" || size <= 0 {
		return "", fmt.Errorf("缺少 sha1 / size，无法秒传")
	}
	if targetDirID == "" {
		targetDirID = "0"
	}
	userkey, err := c.UploadKey(ctx)
	if err != nil {
		return "", err
	}
	userid := c.UID(ctx)
	if userid == "" {
		return "", fmt.Errorf("拿不到账号的 115 uid")
	}
	target := "U_1_" + targetDirID

	form, ts := uploadForm(userid, userkey, sha1sum, name, size, target, uploadAppVer)
	resp, err := c.postUploadInit(ctx, form, ts)
	if err != nil {
		return "", err
	}
	status := num(resp, "status")

	if status == uploadStatusNeedSig {
		need := str(resp, "sign_check")
		signKey := str(resp, "sign_key")
		if fetcher == nil {
			return "", fmt.Errorf("115 要求二次校验（%s），但拿不到源文件内容", need)
		}
		data, ferr := fetcher(need)
		if ferr != nil {
			return "", fmt.Errorf("读取校验数据失败：%w", ferr)
		}
		sum := sha1.Sum(data)
		form["sign_key"] = signKey
		form["sign_val"] = strings.ToUpper(hex.EncodeToString(sum[:]))
		// 带上 sign_key/sign_val 后 token 变了，t 也要重新取
		ts = stampForm(form, userid, sha1sum, uploadAppVer)
		resp, err = c.postUploadInit(ctx, form, ts)
		if err != nil {
			return "", err
		}
		status = num(resp, "status")
	}

	switch status {
	case uploadStatusReuse, uploadStatusOK:
		// reuse=2 是秒传；1 表示 115 上没这份内容、要真上传。
		// 这里只做秒传，status=1 说明源文件内容 115 那边没有，直接报到页面上。
		if status == uploadStatusOK {
			return "", fmt.Errorf("%w（status=1，只能真上传）", ErrInstantNoContent)
		}
		log.Printf("秒传：%s 初始化通过（status=%d statuscode=%d pickcode=%s）",
			name, status, num(resp, "statuscode"), str(resp, "pickcode"))
	default:
		msg := str(resp, "statusmsg")
		if msg == "" {
			msg = str(resp, "message")
		}
		if msg == "" {
			msg = fmt.Sprintf("status=%d", status)
		}
		return "", fmt.Errorf("秒传被拒：%s（statuscode=%d status=%d）", msg, num(resp, "statuscode"), status)
	}

	// 秒传成功时响应里直接带 pickcode，用它就行 —— 别再去列目录：
	// 115 建文件偶尔异步，列目录要轮询，一次播放白等十几秒。
	if pc := str(resp, "pickcode"); pc != "" {
		return pc, nil
	}

	// 响应里没给（旧接口）时才退回列目录确认，轮询间隔也缩短。
	deadline := time.Now().Add(10 * time.Second)
	for {
		if n, err := c.FindByName(ctx, targetDirID, name); err == nil && n != nil && !n.IsDir {
			if n.Pickcode == "" {
				if d, derr := c.Details(ctx, n.ID); derr == nil {
					n.Pickcode = d.Pickcode
				}
			}
			if n.Pickcode != "" {
				return n.Pickcode, nil
			}
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("秒传接口返回成功，但目标目录里没找到「%s」（可能被 115 风控拦截）", name)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

// uploadForm 按官方客户端的算法拼出 sig 与 token。
//
//	sig   = SHA1(userkey + hex(SHA1(userid+fileid+target+"0")) + "000000").upper()
//	token = MD5(盐 + fileid+filesize+sign_key+sign_val+userid+t + hex(MD5(userid)) + appversion)
//
// 注意两点（都是踩出来的）：
//   - t 用**秒**，k_ec 里编码的时间戳也必须是同一份秒值；
//   - appid / sign_key / sign_val 在没值时不要出现，官方是 `if v` 过滤掉的。
func uploadForm(userid, userkey, fileid, filename string, filesize int64, target, appver string) (map[string]string, int64) {
	nowSec := time.Now().Unix()
	ts := strconv.FormatInt(nowSec, 10)
	form := map[string]string{
		"userid":     userid,
		"filename":   filename,
		"fileid":     fileid,
		"filesize":   strconv.FormatInt(filesize, 10),
		"target":     target,
		"topupload":  "true",
		"appversion": appver,
		"t":          ts,
	}
	inner := sha1.Sum([]byte(userid + fileid + target + "0"))
	sig := sha1.New()
	sig.Write([]byte(userkey))
	sig.Write([]byte(hex.EncodeToString(inner[:])))
	sig.Write([]byte("000000"))
	form["sig"] = strings.ToUpper(hex.EncodeToString(sig.Sum(nil)))

	stampForm(form, userid, fileid, appver)
	return form, nowSec
}

// stampForm 刷新表单里的 t 与 token（token 依赖 t 和 sign_key/sign_val），
// 返回本次用的秒级时间戳，供 k_ec 编码使用。
func stampForm(form map[string]string, userid, fileid, appver string) int64 {
	nowSec := time.Now().Unix()
	stampToken(form, userid, fileid, appver, strconv.FormatInt(nowSec, 10))
	return nowSec
}

// stampToken 用指定的 t 字符串重算 token。t 的单位（秒/毫秒）必须与 k_ec 一致，
// 探针要试毫秒时就走这里。
func stampToken(form map[string]string, userid, fileid, appver, ts string) {
	form["t"] = ts

	tok := md5.New()
	tok.Write([]byte(uploadMD5Salt))
	tok.Write([]byte(fileid + form["filesize"] + form["sign_key"] + form["sign_val"] + userid + ts))
	uidSum := md5.Sum([]byte(userid))
	tok.Write([]byte(hex.EncodeToString(uidSum[:])))
	tok.Write([]byte(appver))
	form["token"] = hex.EncodeToString(tok.Sum(nil))
}

// postUploadInit 发一次 4.0 初始化。参数要 AES 加密后当 body 发，
// k_ec 放 query；响应也要解密 + LZ4 解压。
func (c *Client) postUploadInit(ctx context.Context, form map[string]string, ts int64) (map[string]any, error) {
	enc, err := newUploadCipher()
	if err != nil {
		return nil, err
	}
	body, err := encodeUploadPayload(enc, form)
	if err != nil {
		return nil, err
	}
	u := UploadAPI + pathUploadInit + "?k_ec=" + enc.encodeToken(ts)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", uploadUA)
	// 官方就是用 form 的 Content-Type 把密文当 body 发
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if c.Cookies != "" {
		req.Header.Set("Cookie", c.Cookies)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	plain, derr := enc.decrypt(raw)
	if derr != nil {
		// 解密失败时把原文吐出来，方便判断是 401 还是接口又变了
		if m, perr := parseJSON(raw); perr == nil {
			return m, nil
		}
		return nil, fmt.Errorf("秒传初始化响应解不开（HTTP %d）：%s", resp.StatusCode, truncate(string(raw), 160))
	}
	m, perr := parseJSON(plain)
	if perr != nil {
		return nil, fmt.Errorf("秒传初始化返回看不懂：%s", truncate(string(plain), 160))
	}
	return m, nil
}

// PickcodeOf 取文件 pickcode（秒传后写 strm 用）。
func (c *Client) PickcodeOf(ctx context.Context, cid, name string) (string, string, error) {
	n, err := c.FindByName(ctx, cid, name)
	if err != nil {
		return "", "", err
	}
	if n == nil {
		return "", "", fmt.Errorf("目录里没有「%s」", name)
	}
	if n.Pickcode == "" {
		if d, err := c.Details(ctx, n.ID); err == nil {
			n.Pickcode = d.Pickcode
		}
	}
	if n.Pickcode == "" {
		return "", "", fmt.Errorf("拿不到「%s」的 pickcode", name)
	}
	return n.ID, n.Pickcode, nil
}
