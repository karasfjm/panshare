// 改「企业可信IP」（对齐 Python _update_agents/_update_one 的踩坑结论）：
//   - 应用列表路由是 frame#/apps/list，列表项标题 .app_index_item_title
//     没有 href/data-id，只能点进去从地址栏读 #apps/modApiApp/<id>；
//   - 只扫 .js_app_section_openapi_app 分区（前面的「基础」区有 20+ 个
//     官方应用，只取前 N 个标题会全耗在它们身上）；
//   - 「配置」按钮是 div.js_show_ipConfig_dialog（不是 <a>，只匹配
//     “配置”文案会点到隔壁「可信域名」）；
//   - 已配 IP 在弹窗的 textarea value 里（innerText 里没有），必须先开弹窗；
//   - 填 IP 是**追加**不是覆盖，分号分隔，超 50 条裁旧的（企微上限 120）。
package core

import (
	"fmt"
	"log"
	"regexp"
	"strings"
	"time"
)

// 选择器序列（从最精确到最宽松，对齐 Python CONFIG_LOCATORS 等）。
var (
	configSelectors = []string{
		".js_show_ipConfig_dialog",
		"[class*='js_show_ipConfig_dialog']",
	}
	textareaSelectors = []string{
		".js_ipConfig_textarea",
		".app_ipConfig_dialog textarea",
		"div[class*=dialog] textarea",
		"textarea",
	}
	confirmSelectors = []string{
		".js_ipConfig_confirmBtn",
		"[class*='app_ipConfig_dialog'] [class*='confirm']",
	}
	agentTitleRe = regexp.MustCompile(`modApiApp/(\d{3,})`)
)

// discoverAgents 从「应用管理 → 应用」列表抓自建应用的 (名字, AgentId)。
func (m *Manager) discoverAgents(b *browser, stop <-chan struct{}) [][2]string {
	if err := b.open(FRAME_URL+"#/apps/list", 6*time.Second); err != nil {
		log.Printf("[wxip] 打开应用列表失败: %v", err)
		return nil
	}
	titles := b.evalStrings(`(()=>{
` + jsDocs + `
const out=[];
for(const doc of docs){
  document.querySelectorAll('.js_app_section_openapi_app .app_index_item_title').forEach(e=>{
    const t=(e.innerText||'').trim();
    if(t&&!t.startsWith('创建应用'))out.push(t);
  });
}
return out;})()`)
	log.Printf("[wxip] 自建应用分区：%s", strings.Join(titles, "、"))
	var out [][2]string
	seen := map[string]bool{}
	for _, name := range titles {
		select {
		case <-stop:
			return out
		default:
		}
		if err := b.open(FRAME_URL+"#/apps/list", 3*time.Second); err != nil {
			continue
		}
		if !b.clickByTextInFrames(".js_app_section_openapi_app", name) {
			continue
		}
		select {
		case <-stop:
			return out
		case <-time.After(1500 * time.Millisecond):
		}
		if match := agentTitleRe.FindStringSubmatch(b.location()); match != nil && !seen[match[1]] {
			seen[match[1]] = true
			out = append(out, [2]string{name, match[1]})
		}
	}
	return out
}

// agentResult 单个应用的更新结果（前端 last_update.agents / detail 拼接用）。
type agentResult struct {
	Agent  string `json:"agent"`
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail"`
	Old    string `json:"old,omitempty"`
}

// updateAgents 把 ip 追加进（全部或指定）自建应用的可信 IP。
func (m *Manager) updateAgents(stop <-chan struct{}, s Settings, agents []string, ip string) map[string]any {
	var results []agentResult
	err := m.withSession(10*time.Minute, m.loadState(), func(b *browser) error {
		if err := b.open(FRAME_URL, 2*time.Second); err != nil {
			return err
		}
		if b.onLoginPage() {
			m.setLogin(LoginExpired)
			results = []agentResult{}
			return nil
		}
		type pair struct{ name, id string }
		var pairs []pair
		if len(agents) > 0 {
			for _, a := range agents {
				if isDigits(a) {
					pairs = append(pairs, pair{a, a})
				}
			}
			var named []string
			for _, a := range agents {
				if !isDigits(a) {
					named = append(named, a)
				}
			}
			if len(named) > 0 {
				found := m.discoverAgents(b, stop)
				byName := map[string]string{}
				for _, f := range found {
					byName[strings.ToLower(f[0])] = f[1]
				}
				for _, nm := range named {
					if hit := byName[strings.ToLower(nm)]; hit != "" {
						pairs = append(pairs, pair{nm, hit})
					} else {
						results = append(results, agentResult{Agent: nm, Name: nm,
							Detail: "后台里没找到这个应用名"})
					}
				}
			}
		} else {
			found := m.discoverAgents(b, stop)
			if len(found) == 0 {
				results = []agentResult{}
				return fmt.Errorf("没读到应用列表，请在配置里直接填 AgentId")
			}
			names := make([]string, 0, len(found))
			for _, f := range found {
				pairs = append(pairs, pair{f[0], f[1]})
				names = append(names, f[0])
			}
			log.Printf("[wxip] 自动发现 %d 个自建应用：%s", len(found), strings.Join(names, "、"))
		}
		for _, p := range pairs {
			select {
			case <-stop:
				return nil
			default:
			}
			results = append(results, m.updateOne(b, p.id, p.name, ip))
			select {
			case <-stop:
				return nil
			case <-time.After(time.Second):
			}
		}
		return nil
	})
	if err != nil {
		return map[string]any{"ok": false, "detail": "更新失败：" + err.Error(), "agents": results}
	}
	if results == nil {
		results = []agentResult{}
	}
	ok := len(results) > 0
	details := make([]string, 0, len(results))
	for _, r := range results {
		if !r.OK {
			ok = false
		}
		label := r.Name
		if label == "" {
			label = r.Agent
		}
		if r.OK {
			details = append(details, label+"：成功")
		} else {
			details = append(details, label+"："+r.Detail)
		}
	}
	return map[string]any{"ok": ok, "detail": strings.Join(details, "；"), "agents": results}
}

// updateOne 更新单个应用的可信 IP（追加、校验回读）。
func (m *Manager) updateOne(b *browser, agentID, name, ip string) agentResult {
	label := name
	if label == "" {
		label = agentID
	}
	ret := func(ok bool, detail string) agentResult {
		return agentResult{Agent: agentID, Name: label, OK: ok, Detail: detail}
	}
	url := fmt.Sprintf("%s#apps/modApiApp/%s", FRAME_URL, agentID)
	if err := b.open(url, 3*time.Second); err != nil {
		return ret(false, fmt.Sprintf("%v", err))
	}
	if b.onLoginPage() {
		return ret(false, "登录态已失效")
	}
	_ = b.bodyTextContains("可信", 8*time.Second)
	if !b.clickInFrames(configSelectors) {
		_ = m.dumpPage(b)
		return ret(false, "没找到「企业可信IP」的配置按钮（已存 last.html）")
	}
	// 等弹窗真的出来，否则读 textarea 会扑空
	waitVisible(b, textareaSelectors[0], 8*time.Second)
	old, _ := b.textareaValue(textareaSelectors)
	if strings.Contains(old, ip) {
		b.closeDialog()
		return ret(true, fmt.Sprintf("已经是 %s，跳过", ip))
	}
	newVal := appendIP(old, ip)
	if !b.setTextareaValue(textareaSelectors, newVal) {
		_ = m.dumpPage(b)
		return ret(false, "没找到 IP 输入框")
	}
	if !b.clickInFrames(confirmSelectors) {
		_ = m.dumpPage(b)
		return ret(false, "没点到「确定」")
	}
	// 关掉弹窗后重新打开读一次：比「认为点成功了」可靠
	time.Sleep(2 * time.Second)
	now := ""
	if b.clickInFrames(configSelectors) {
		waitVisible(b, textareaSelectors[0], 8*time.Second)
		now, _ = b.textareaValue(textareaSelectors)
		b.closeDialog()
	}
	if strings.Contains(now, ip) {
		return ret(true, "已更新")
	}
	return ret(false, fmt.Sprintf("页面显示的还是 %s", orEmpty(now, "（读不到）")))
}

// appendIP 把 ip 追加进已有分号分隔列表（对齐 Python _fill_ip 的逻辑，
// 放在 Go 里做拆并、JS 只负责写入）。
func appendIP(cur, ip string) string {
	var items []string
	for _, x := range strings.FieldsFunc(cur, func(r rune) bool {
		return r == ';' || r == '；' || r == ',' || r == '，' ||
			r == ' ' || r == '\t' || r == '\n' || r == '\r'
	}) {
		if x != "" {
			items = append(items, x)
		}
	}
	for _, x := range items {
		if x == ip {
			return cur
		}
	}
	items = append(items, ip)
	if len(items) > 50 { // 企微上限 120，留点余量
		items = items[len(items)-50:]
	}
	return strings.Join(items, ";")
}

func orEmpty(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

// waitVisible 轮询等元素可见（等价 page.wait_for_selector(state="visible")）。
func waitVisible(b *browser, sel string, timeout time.Duration) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if b.evalString(fmt.Sprintf(`(()=>{
%s
const el=document.querySelector(%q);
return (el&&el.offsetWidth>0)?'ok':'';})()`, jsDocs, sel)) == "ok" {
			return
		}
		select {
		case <-b.ctx.Done():
			return
		case <-time.After(500 * time.Millisecond):
		}
	}
}
