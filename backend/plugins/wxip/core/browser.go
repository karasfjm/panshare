// chromedp 浏览器会话层 —— Python 版用 playwright，Go 版用 chromedp
// （纯 Go 的 CDP 客户端，驱动系统装的 Chromium/Chrome，不自己下载浏览器）。
//
// 企微管理后台把内容放在同源 iframe 里，这里统一用「注入 JS、在
// main document 里遍历 contentDocument」的方式跨 frame 定位元素 ——
// 等价于 Python _find(page, selectors) 遍历 page.frames。
package core

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"

	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/chromedp"
)

// FRAME_URL / LOGIN_URL 对齐 Python 常量。
const (
	FRAME_URL = "https://work.weixin.qq.com/wework_admin/frame"
	LOGIN_URL = "https://work.weixin.qq.com/wework_admin/loginpage_wx?from=myhome"
)

// 登录票据 cookie：企微登录成功后一定会下发 wwrtx.vst，比看 URL 靠谱。
const ticketCookie = "wwrtx.vst"

// ---------------------------------------------------------------- 探测

// chromiumCandidates 常见安装路径（按平台）。
func chromiumCandidates() []string {
	switch runtime.GOOS {
	case "windows":
		return []string{
			`C:\Program Files\Google\Chrome\Application\chrome.exe`,
			`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
			`C:\Program Files (x86)\Chromium\Application\chromium.exe`,
			`C:\Program Files\Chromium\Application\chromium.exe`,
		}
	case "darwin":
		return []string{
			"/Applications/Chromium.app/Contents/MacOS/Chromium",
			"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
		}
	default: // linux
		return []string{
			"/usr/bin/chromium",
			"/usr/bin/chromium-browser",
			"/usr/bin/google-chrome",
			"/usr/bin/google-chrome-stable",
			"/usr/local/bin/chromium",
			"/snap/bin/chromium",
			"/opt/google/chrome/chrome",
		}
	}
}

// FindChromium 找一个可用的浏览器内核：CHROME_PATH 环境变量最优先，
// 然后 PATH，最后常见安装路径。找不到返回空串。
func FindChromium() string {
	if p := strings.TrimSpace(os.Getenv("CHROME_PATH")); p != "" {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	names := []string{"chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"}
	for _, name := range names {
		if p, err := exec.LookPath(name); err == nil {
			return p
		}
	}
	for _, p := range chromiumCandidates() {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

// ChromiumAvailable 是否具备跑浏览器的能力。
func ChromiumAvailable() bool { return FindChromium() != "" }

// ---------------------------------------------------------------- 会话

// browser 是一次浏览器会话（用完即关，不常驻 —— 对齐 Python
// 「常驻一个 Chromium 白吃 150MB 内存，不如冷启动两秒」的取舍）。
type browser struct {
	ctx    context.Context
	cancel context.CancelFunc
}

// withSession 起一个 headless 会话跑 fn，结束（或超时）连浏览器一起回收。
// cookies 非空时先注入再导航（等价 playwright 的 storage_state）。
func (m *Manager) withSession(timeout time.Duration, cookies []cookieJSON,
	fn func(b *browser) error) error {

	exe := FindChromium()
	if exe == "" {
		return errors.New("没找到 Chromium/Chrome：apt install chromium 或设 CHROME_PATH")
	}
	s := m.Settings()
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.ExecPath(exe),
		chromedp.Flag("no-sandbox", true),
		chromedp.Flag("disable-dev-shm-usage", true),
		chromedp.Flag("disable-gpu", true),
		chromedp.Flag("headless", true),
		chromedp.Flag("lang", "zh-CN"),
		chromedp.WindowSize(1440, 900),
	)
	if s.Proxy != "" {
		opts = append(opts, chromedp.ProxyServer(s.Proxy))
	}
	allocCtx, allocCancel := chromedp.NewExecAllocator(context.Background(), opts...)
	defer allocCancel()
	ctx, ctxCancel := chromedp.NewContext(allocCtx)
	defer ctxCancel()
	runCtx, runCancel := context.WithTimeout(ctx, timeout)
	defer runCancel()

	b := &browser{ctx: runCtx, cancel: runCancel}
	// 先起 target 再注 cookie（SetCookies 需要一个页面）
	if err := chromedp.Run(runCtx, chromedp.Tasks{}); err != nil {
		return fmt.Errorf("启动浏览器失败: %w", err)
	}
	if len(cookies) > 0 {
		if err := chromedp.Run(runCtx, b.setCookiesAction(cookies)); err != nil {
			return fmt.Errorf("恢复登录态失败: %w", err)
		}
	}
	if err := fn(b); err != nil {
		return err
	}
	return nil
}

// setCookiesAction 把磁盘登录态注进浏览器。
func (b *browser) setCookiesAction(cookies []cookieJSON) chromedp.Action {
	return chromedp.ActionFunc(func(ctx context.Context) error {
		params := make([]*network.CookieParam, 0, len(cookies))
		for _, c := range cookies {
			if c.Domain == "" {
				continue
			}
			params = append(params, &network.CookieParam{
				Name: c.Name, Value: c.Value,
				Domain: c.Domain, Path: c.Path,
				Secure: c.Secure, HTTPOnly: c.HTTPOnly,
			})
		}
		if len(params) == 0 {
			return nil
		}
		return network.SetCookies(params).Do(ctx)
	})
}

// collectCookies 抓当前全部 cookie（存登录态用）。
func (b *browser) collectCookies() []cookieJSON {
	var out []cookieJSON
	_ = chromedp.Run(b.ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		cs, err := network.GetCookies().Do(ctx)
		if err != nil {
			return err
		}
		for _, c := range cs {
			if !strings.Contains(c.Domain, "work.weixin.qq.com") {
				continue
			}
			out = append(out, cookieJSON{
				Name: c.Name, Value: c.Value, Domain: c.Domain, Path: c.Path,
				Expires:  float64(c.Expires),
				HTTPOnly: c.HTTPOnly, Secure: c.Secure,
			})
		}
		return nil
	}))
	return out
}

// open 打开页面并等 SPA 渲染一会（企微后台是 hash 路由的 SPA，
// domcontentloaded 之后还要跑几秒 JS）。
func (b *browser) open(url string, settle time.Duration) error {
	if err := chromedp.Run(b.ctx, chromedp.Navigate(url)); err != nil {
		return err
	}
	select {
	case <-b.ctx.Done():
		return b.ctx.Err()
	case <-time.After(settle):
	}
	return nil
}

// location 读当前完整 URL（含 hash，SPA 路由变化能看到）。
func (b *browser) location() string {
	var u string
	_ = chromedp.Run(b.ctx, chromedp.Location(&u))
	return u
}

// onLoginPage 登录页判定（对齐 Python：URL 含 loginpage）。
func (b *browser) onLoginPage() bool {
	return strings.Contains(strings.ToLower(b.location()), "loginpage")
}

// hasTicket cookie 里有没有 wwrtx.vst（HttpOnly，得走 CDP 拿）。
func (b *browser) hasTicket() bool {
	found := false
	_ = chromedp.Run(b.ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		cs, err := network.GetCookies().Do(ctx)
		if err != nil {
			return err
		}
		for _, c := range cs {
			if c.Name == ticketCookie && strings.TrimSpace(c.Value) != "" {
				found = true
				break
			}
		}
		return nil
	}))
	return found
}

// loggedIn 已登录 = 有票据 cookie，或 URL 已离开登录页（辅助判据，
// 对齐 Python _logged_in 的注释）。
func (b *browser) loggedIn() bool {
	if b.hasTicket() {
		return true
	}
	u := strings.ToLower(b.location())
	return u != "" && !strings.Contains(u, "loginpage")
}

// screenshotPNG 截当前视口。
func (b *browser) screenshotPNG() ([]byte, error) {
	var buf []byte
	if err := chromedp.Run(b.ctx, chromedp.CaptureScreenshot(&buf)); err != nil {
		return nil, err
	}
	return buf, nil
}

// pngDataURL 截图转 data URL（前端 <img src> 直接用）。
func pngDataURL(raw []byte) string {
	return "data:image/png;base64," + base64.StdEncoding.EncodeToString(raw)
}

// ---------------------------------------------------------------- JS 助手

// jsDocs 是跨 frame 收集 document 的 JS 片段（同源 iframe 可以直接
// 摸 contentDocument；企微后台所有 frame 都在 work.weixin.qq.com 下）。
const jsDocs = `const docs=[document];
try{for(const f of document.querySelectorAll('iframe')){try{if(f.contentDocument)docs.push(f.contentDocument);}catch(e){}}}catch(e){}`

// evalString 跑一段返回字符串的 JS。
func (b *browser) evalString(js string) string {
	var out string
	if err := chromedp.Run(b.ctx, chromedp.Evaluate(js, &out)); err != nil {
		log.Printf("[wxip] evaluate 失败: %v", err)
		return ""
	}
	return out
}

// evalStrings 跑一段返回字符串数组的 JS。
func (b *browser) evalStrings(js string) []string {
	var out []string
	if err := chromedp.Run(b.ctx, chromedp.Evaluate(js, &out)); err != nil {
		log.Printf("[wxip] evaluate 失败: %v", err)
		return nil
	}
	return out
}

// clickInFrames 在任意 frame 里找 selector 点一下，返回是否点到。
// extra 是可选的 JS 代码（在拿到 el 之后执行，比如 scrollIntoView）。
func (b *browser) clickInFrames(selectors []string) bool {
	for _, sel := range selectors {
		js := fmt.Sprintf(`(()=>{
%[1]s
for(const doc of docs){
  const el=doc.querySelector(%[2]q);
  if(el){try{el.scrollIntoView({block:'center'});}catch(e){}
    el.click();return 'ok';}
}
return '';})()`, jsDocs, sel)
		if b.evalString(js) == "ok" {
			return true
		}
	}
	return false
}

// clickByTextInFrames 按「元素的 innerText 精确等于 text」跨 frame 点击
// （企微列表项没有 href / data-id，只能按文案点）。
func (b *browser) clickByTextInFrames(containerSel, text string) bool {
	raw, _ := json.Marshal(text)
	js := fmt.Sprintf(`(()=>{
%[1]s
const want=(%[2]s);
for(const doc of docs){
  const box=doc.querySelector(%[3]q)||doc;
  const els=[...box.querySelectorAll('*')];
  const hit=els.filter(e=>(e.innerText||e.textContent||'').trim()===want).pop();
  if(hit){hit.click();return 'ok';}
}
return '';})()`, jsDocs, string(raw), containerSel)
	return b.evalString(js) == "ok"
}

// textareaValue 跨 frame 读第一个可见 textarea 的值；second 返回
// 「弹窗在但没配任何 IP」（value 空串）与「根本没找到」的区分。
func (b *browser) textareaValue(selectors []string) (value string, found bool) {
	for _, sel := range selectors {
		js := fmt.Sprintf(`(()=>{
%[1]s
for(const doc of docs){
  const el=doc.querySelector(%[2]q);
  if(el&&el.offsetWidth>0){return 'V'+(el.value||'');}
}
return '';})()`, jsDocs, sel)
		out := b.evalString(js)
		if strings.HasPrefix(out, "V") {
			return strings.TrimPrefix(out, "V"), true
		}
	}
	return "", false
}

// setTextareaValue 跨 frame 找 textarea 写值并派发 input/change
// （企微的确定按钮会按 value 读，光 set value 不派发事件可能不认）。
func (b *browser) setTextareaValue(selectors []string, value string) bool {
	raw, _ := json.Marshal(value)
	for _, sel := range selectors {
		js := fmt.Sprintf(`(()=>{
%[1]s
for(const doc of docs){
  const el=doc.querySelector(%[2]q);
  if(el&&el.offsetWidth>0){
    el.focus();el.value=(%[3]s);
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    return 'ok';}
}
return '';})()`, jsDocs, sel, string(raw))
		if b.evalString(js) == "ok" {
			return true
		}
	}
	return false
}

// ------------------------------------------------------------ 验证码交互
// 企微后台登录偶尔会触发安全验证（短信/图形验证码）。原版 Python 用
// WebSocket 推图片，这里浏览器只能在单一 goroutine 里操作，所以改成：
// 登录轮询里检测到验证码框 → 截图存到 Manager → 前端轮询拿图 → 用户填
// code → 网页 API 置标志 → 轮询循环里消费并回填提交。

// verifySelectors 定位验证码输入框（对齐 Python _verify_detected）。
var verifySelectors = []string{
	"input[placeholder*='验证码']", "input[name*='code']", "input[name*='verify']",
	"#verifyCode", "input[placeholder*='验证']",
}

// verifyVisible 任意 frame 里出现可见的验证码输入框就返回 true。
func (b *browser) verifyVisible() bool {
	sels, _ := json.Marshal(verifySelectors)
	js := fmt.Sprintf(`(()=>{
%s
const sels=%s;
for(const doc of docs){
  for(const s of sels){
    const el=doc.querySelector(s);
    if(el&&el.offsetWidth>0)return 'yes';
  }
}
return '';})()`, jsDocs, string(sels))
	return b.evalString(js) == "yes"
}

// clickTextLikeInFrames 跨 frame 按 innerText 精确等于某个文案点击（取
// 最后一个匹配，通常是按钮而非外层容器），用于「获取验证码 / 确认」。
func (b *browser) clickTextLikeInFrames(words []string) bool {
	raw, _ := json.Marshal(words)
	js := fmt.Sprintf(`(()=>{
%s
const words=%s;
for(const w of words){
  for(const doc of docs){
    const els=[...doc.querySelectorAll('button,a,span,div,input[type=button]')];
    const hit=els.filter(e=>e.offsetWidth>0&&e.offsetHeight>0&&(e.innerText||e.value||'').trim()===w).pop();
    if(hit){try{hit.click();}catch(e){}return 'ok';}
  }
}
return '';})()`, jsDocs, string(raw))
	return b.evalString(js) == "ok"
}

// sendVerifyCode 尝试点「获取验证码」，成功返回 true。
func (b *browser) sendVerifyCode() bool {
	return b.clickTextLikeInFrames([]string{"获取验证码", "发送验证码", "重新获取验证码", "获取", "发送"})
}

// captureVerifyImage 截图验证码：优先抓 img[verify/captcha] 画到 canvas，
// 抓不到就整页截图兜底（短信验证码没有图片，整页截图能让用户看到状态）。
func (b *browser) captureVerifyImage() string {
	js := fmt.Sprintf(`(()=>{
%s
const sels=["img[src*='verify']","img[src*='captcha']",".verify-img img","img[class*='captcha']"];
for(const doc of docs){
  for(const s of sels){
    const el=doc.querySelector(s);
    if(el&&el.offsetWidth>0){
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
return '';})()`, jsDocs)
	if d := b.evalString(js); d != "" {
		return d
	}
	if raw, err := b.screenshotPNG(); err == nil {
		return pngDataURL(raw)
	}
	return ""
}

// fillAndSubmitVerify 把用户填的验证码写进输入框并提交（对齐 Python
// submit_verification_code：填值 → 点确认/登录，找不到按钮就按回车）。
func (b *browser) fillAndSubmitVerify(code string) {
	_ = b.setTextareaValue(verifySelectors, code)
	if b.clickTextLikeInFrames([]string{"确认", "确定", "提交", "验证", "登录", "验证并登录"}) {
		return
	}
	// 兜底：向每个 frame 的焦点元素派发 Enter
	b.evalString(`(()=>{
` + jsDocs + `
for(const doc of docs){
  const t=doc.activeElement||doc.body;
  if(!t)continue;
  for(const type of ['keydown','keypress','keyup']){
    try{t.dispatchEvent(new KeyboardEvent(type,{key:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));}catch(e){}
  }
}
return 'ok';})()`)
}

// closeDialog 关掉弹窗但不保存（对齐 Python 按 Escape：向每个 frame 的
// 焦点元素派发 Escape 键事件）。
func (b *browser) closeDialog() {
	b.evalString(`(()=>{
` + jsDocs + `
for(const doc of docs){
  const target=doc.activeElement||doc.body;
  if(!target)continue;
  for(const type of ['keydown','keyup']){
    try{target.dispatchEvent(new KeyboardEvent(type,{key:'Escape',keyCode:27,which:27,bubbles:true,cancelable:true}));}catch(e){}
  }
}
return 'ok';})()`)
	select {
	case <-b.ctx.Done():
	case <-time.After(500 * time.Millisecond):
	}
}

// bodyTextContains 等 body 文案出现某个词（等价 Python 的
// wait_for_function(innerText.includes(...))，轮询实现，不依赖事件）。
func (b *browser) bodyTextContains(word string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if strings.Contains(b.evalString(`(()=>{const t=document.body?document.body.innerText:'';return t;})()`), word) {
			return true
		}
		select {
		case <-b.ctx.Done():
			return false
		case <-time.After(500 * time.Millisecond):
		}
	}
	return false
}
