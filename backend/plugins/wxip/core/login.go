// 扫码登录企微管理后台（对齐 Python _login_flow）：
// 已有登录态先用 cookie 试开一次后台，能用就不折腾用户扫码；
// 否则开登录页截二维码（二维码在 iframe 里，靠跨 frame JS 抓成
// data URL），手机扫码确认后轮询 wwrtx.vst 票据，最多等 5 分钟。
package core

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"time"
)

// qrSelectors 定位登录二维码（对齐 Python _wait_qr）。
var qrSelectors = []string{
	"img.qrcode_login_img", "img.js_qrcode_img",
	"img[src*='qrcode']", ".qrcode img", "canvas.qrcode",
}

// qrDataURL 跨 frame 抓二维码元素并转 data URL；抓不到返回空串。
// img 元素画到 canvas 上再 toDataURL（同源不会污染 canvas）。
func (b *browser) qrDataURL() string {
	sels, _ := json.Marshal(qrSelectors)
	js := fmt.Sprintf(`(()=>{
%s
const sels=%s;
for(const doc of docs){
  for(const sel of sels){
    const el=doc.querySelector(sel);
    if(el&&el.offsetWidth>50){
      if(el.tagName==='CANVAS'){try{return el.toDataURL('image/png');}catch(e){}}
      try{
        const c=doc.createElement('canvas');
        c.width=el.naturalWidth||el.width;c.height=el.naturalHeight||el.height;
        c.getContext('2d').drawImage(el,0,0);
        const d=c.toDataURL('image/png');
        if(d.startsWith('data:image'))return d;
      }catch(e){}
    }
  }
}
return '';})()`, jsDocs, sels)
	return b.evalString(js)
}

// waitQR 最多 25 秒等二维码出现。
func (b *browser) waitQR(stop <-chan struct{}) string {
	deadline := time.Now().Add(25 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-stop:
			return ""
		default:
		}
		if d := b.qrDataURL(); d != "" {
			return d
		}
		select {
		case <-stop:
			return ""
		case <-time.After(time.Second):
		}
	}
	return ""
}

// loginFlow 完整登录流程（只在该跑的 goroutine 里调）。
func (m *Manager) loginFlow(stop <-chan struct{}, s Settings) {
	m.setLogin(LoginQR)
	m.setError("")
	m.clearVerify()
	// 浏览器可能还在首次下载中：最多等 6 分钟，装好再开会话。
	// 这段等待不占浏览器会话，也不阻塞主服务的健康检查。
	if FindChromium() == "" {
		m.setLogin(LoginWaiting)
		waitDeadline := time.Now().Add(6 * time.Minute)
		for FindChromium() == "" && time.Now().Before(waitDeadline) {
			select {
			case <-stop:
				return
			case <-time.After(2 * time.Second):
			}
		}
		if FindChromium() == "" {
			m.setError("浏览器还没下载好，请稍后再点「扫码登录」")
			m.setLogin(LoginError)
			return
		}
		m.setLogin(LoginQR)
	}
	err := m.withSession(8*time.Minute, m.loadState(), func(b *browser) error {
		// 已有登录态：先直接用已有 Cookie 开一次后台，能用就不折腾扫码
		//（对齐 Python：否则已登录的人点「扫码登录」会被跳到 frame#/ 报没二维码）
		if m.stateExists() {
			if err := b.open(FRAME_URL, 3*time.Second); err == nil && b.loggedIn() {
				m.setLogin(LoginOK)
				m.mu.Lock()
				m.qrB64 = ""
				m.mu.Unlock()
				log.Printf("[wxip] 已有登录态，跳过扫码（url=%.100s）", b.location())
				return nil
			}
			log.Printf("[wxip] 登录态已失效，重新扫码")
			_ = os.Remove(m.stateFile())
			// 旧 cookie 还在浏览器里，这次 withSession 起的是带旧 cookie 的会话，
			// 但登录页会重新走，二维码流程不受影响
		}
		if err := b.open(LOGIN_URL, 2*time.Second); err != nil {
			return err
		}
		img := b.waitQR(stop)
		if img == "" {
			_ = m.dumpPage(b)
			return errors.New("没找到登录二维码，可能页面结构变了（已保存 last.html）")
		}
		m.mu.Lock()
		m.qrB64 = img
		m.qrAt = time.Now()
		m.mu.Unlock()
		m.setLogin(LoginWaiting)
		log.Printf("[wxip] 企微登录二维码已就绪，等待手机扫码确认（最多 5 分钟）")

		// 扫码后最多等 5 分钟（手机端还要点一次「确认登录」）。
		// 中途若企微触发安全验证，会切到 verify 状态等用户填验证码；
		// 验证码交互可能较慢，检测到后把窗口延长到 8 分钟（与会话一致）。
		deadline := time.Now().Add(5 * time.Minute)
		lastURL := ""
		verifySeen := false
		for time.Now().Before(deadline) {
			select {
			case <-stop:
				return nil
			case <-time.After(2 * time.Second):
			}

			// ① 用户提交了验证码：在浏览器里回填并提交，然后接着等登录结果
			if code := m.takeVerifyCode(); code != "" {
				log.Printf("[wxip] 收到用户提交的验证码，正在回填提交")
				b.fillAndSubmitVerify(code)
				m.mu.Lock()
				m.verifyB64 = ""
				m.verifyMsg = "验证码已提交，等待结果…"
				m.loginState = LoginWaiting
				m.mu.Unlock()
				// 提交后给后台一点反应时间，并把窗口延长
				deadline = time.Now().Add(4 * time.Minute)
				continue
			}

			// ② 登录成功：拿到票据 cookie 才算数
			if b.loggedIn() {
				if !b.hasTicket() {
					return errors.New("登录没有真正成功（没拿到 wwrtx.vst），请在手机上点「确认登录」")
				}
				cookies := b.collectCookies()
				if !hasTicketCookie(cookies) {
					return errors.New("登录没有真正成功（没拿到 wwrtx.vst），请在手机上点「确认登录」")
				}
				if err := m.saveState(cookies); err != nil {
					log.Printf("[wxip] 保存登录态失败: %v", err)
				} else {
					log.Printf("[wxip] 企微管理后台登录成功（登录态已存 %s）", m.stateFile())
				}
				m.setLogin(LoginOK)
				m.mu.Lock()
				m.qrB64 = ""
				m.mu.Unlock()
				m.clearVerify()
				return nil
			}

			// ③ 检测到验证码输入框：点「获取验证码」+ 截图，切到 verify 等用户填
			if !verifySeen && b.verifyVisible() {
				verifySeen = true
				msg := "企微要求安全验证。若是短信验证码，已尝试点「获取验证码」，请查收手机短信后填写；若是图形验证码，请照下方图片输入。"
				sent := b.sendVerifyCode()
				if sent {
					msg = "已点「获取验证码」，请查收手机短信后把验证码填到下面（若是图形验证码请照图输入）。"
				}
				img := b.captureVerifyImage()
				m.setVerify(img, msg)
				deadline = time.Now().Add(8 * time.Minute)
				log.Printf("[wxip] 检测到企微安全验证，已推送验证码截图等待用户输入（sent=%v img=%d bytes）", sent, len(img))
			}

			if u := b.location(); u != "" && u != lastURL {
				lastURL = u
				log.Printf("[wxip] 等扫码登录中：url=%.120s vst=%v", u, b.hasTicket())
			}
		}
		return fmt.Errorf("登录超时（扫码后要在手机上点「确认登录」；若出现安全验证需填验证码）。浏览器最后停在：%s", lastURL)
	})
	if err != nil {
		m.setError(err.Error())
		m.setLogin(LoginError)
		log.Printf("[wxip] 企微扫码登录失败: %v", err)
	}
}

// hasTicketCookie 检查 cookie 列表里有没有非空登录票据。
func hasTicketCookie(cookies []cookieJSON) bool {
	for _, c := range cookies {
		if c.Name == ticketCookie && c.Value != "" {
			return true
		}
	}
	return false
}

// dumpPage 把当前页面 HTML 存到 DATA_DIR/wxip/last.html（选择器失配时排查用）。
func (m *Manager) dumpPage(b *browser) string {
	html := b.evalString(`(()=>document.documentElement?document.documentElement.outerHTML:'')()`)
	if html == "" {
		return ""
	}
	_ = os.MkdirAll(m.dir, 0o755)
	path := m.dir + "/last.html"
	if err := os.WriteFile(path, []byte(html), 0o644); err != nil {
		log.Printf("[wxip] 保存页面失败: %v", err)
		return ""
	}
	return path
}
