package service

// 企业微信自建应用的「自定义菜单」。
//
// 菜单项全部是 click 事件，key 直接写成指令（/status、/inc all…）。
// 成员点一下，企业微信就把 click 事件推到我们的回调，那里按指令执行、
// 再把结果被动回复过去——所以点菜单既不用可信 IP，也不用成员记指令。
//
// 走的是 qyapi 的 menu/create，这个接口和其他接口一样要求服务器出口 IP
// 在「企业可信 IP」白名单里，没配就会报 60020。

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"panshare/internal/model"
)

// 企业微信限制：最多 3 个一级菜单，每个一级菜单最多 5 个二级菜单；
// 一级菜单名字不超过 16 字节，二级不超过 60 字节。
const wxMenuMaxSub = 5

// wxAppMenu 是提交给 menu/create 的结构。
//
// 三个一级菜单：运行（状态/账号/配置/帮助）、全量、增量。
// 后两个除了 all 之外，按生成配置各列一条，点哪条就跑哪条——
// 配置多了超出 5 个就只留前几条，剩下的走 /strm 里的专属指令。
func wxAppMenu(cfgs []model.StrmConfig) map[string]any {
	click := func(name, key string) map[string]any {
		return map[string]any{"type": "click", "name": name, "key": key}
	}

	run := []any{
		click("状态", "/status"),
		click("账号", "/accounts"),
		click("配置", "/strm"),
		click("帮助", "/help"),
	}

	// 每条配置一条菜单：名字用人能看懂的配置名，key 用 /full_<代号>。
	full := []any{click("全量 all", "/full all")}
	inc := []any{click("增量 all", "/inc all")}
	aliases := configAliases(cfgs)
	for _, c := range cfgs {
		if len(full) >= wxMenuMaxSub {
			break
		}
		alias := aliases[c.ID]
		if alias == "" {
			continue
		}
		name := menuName(c.Name, alias)
		full = append(full, click(name, "/full_"+alias))
		inc = append(inc, click(name, "/inc_"+alias))
	}

	return map[string]any{
		"button": []any{
			map[string]any{"name": "运行", "sub_button": run},
			map[string]any{"name": "全量", "sub_button": full},
			map[string]any{"name": "增量", "sub_button": inc},
		},
	}
}

// menuName 把配置名压成一行、并限长，太长的截断加省略号。
// 企业微信二级菜单名上限 60 字节，中文按 12 个字算已够看。
func menuName(name, alias string) string {
	s := strings.Join(strings.Fields(name), " ")
	if s == "" {
		return alias
	}
	r := []rune(s)
	if len(r) > 12 {
		return string(r[:11]) + "…"
	}
	return s
}

// PushMenu 把上面这份菜单提交到企业微信，覆盖应用原有的菜单。
func (a *WeChatApp) PushMenu() error {
	s := a.settings()
	agentID := strings.TrimSpace(s.WeChatAgentID)
	if agentID == "" {
		return fmt.Errorf("还没填 AgentID（应用ID）")
	}
	if strings.TrimSpace(s.WeChatCorpID) == "" || strings.TrimSpace(s.WeChatSecret) == "" {
		return fmt.Errorf("CorpID / Secret 还没填齐，取不到 access_token")
	}

	body, err := json.Marshal(wxAppMenu(a.app.DB.ListConfigs()))
	if err != nil {
		return err
	}

	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		tok, err := a.app.WeChat.accessToken(attempt > 0)
		if err != nil {
			return err
		}
		u := "https://qyapi.weixin.qq.com/cgi-bin/menu/create?access_token=" +
			url.QueryEscape(tok) + "&agentid=" + url.QueryEscape(agentID)
		req, err := http.NewRequest(http.MethodPost, u, bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := a.app.WeChat.httpClient().Do(req)
		if err != nil {
			return a.app.WeChat.scrub(err)
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		_ = resp.Body.Close()

		var out struct {
			ErrCode int    `json:"errcode"`
			ErrMsg  string `json:"errmsg"`
		}
		if err := json.Unmarshal(raw, &out); err != nil {
			return fmt.Errorf("企业微信返回看不懂：%s", snippet(raw))
		}
		if out.ErrCode == 0 {
			_ = a.app.DB.SetMeta("wxapp_menu_at", time.Now().Format("2006-01-02 15:04:05"))
			return nil
		}
		lastErr = fmt.Errorf("errcode %d：%s（%s）", out.ErrCode, out.ErrMsg, wxHint(out.ErrCode))
		// token 失效类的错误，换新 token 再试一次
		switch out.ErrCode {
		case 40014, 42001, 41001:
			a.app.WeChat.invalidateToken()
			continue
		}
		return lastErr
	}
	return lastErr
}
