package service

// Telegram 指令的解析与执行。
//
// 指令分三类：
//  1. 查看：/status、/accounts、/strm
//  2. 生成：/inc、/full，以及每条配置专属的 /inc_<代号>、/full_<代号>
//  3. 链接：直接把 ed2k 或 115 分享链接发进来（也可以用 /ed2k、/share 显式指定），
//     按「ed2k」「分享转存」页面里的设置建任务并生成 strm。
//
// 生成类指令跟网页上「立即增量 / 立即全量」是同一个入口（RunStrm），
// 所以进度、完成事件、失败原因都和手动点按钮一致。

import (
	"fmt"
	"regexp"
	"strings"

	"panshare/internal/model"
)

// 保留字：这些代号不能被某条配置占用，否则 /inc、/full 这类通用指令
// 会被配置专属指令顶掉。
var tgReserved = map[string]bool{
	"all": true, "inc": true, "full": true, "scan": true, "strm": true, "configs": true,
	"status": true, "accounts": true, "start": true, "help": true, "find": true,
	"link": true, "ed2k": true, "share": true,
	"cleanup": true, "cleanup_ok": true, "cleanup_no": true,
}

// configAliases 给每条配置取一个能塞进 Telegram 命令的短代号。
//
// Telegram 只认 [a-z0-9_]，配置名基本是中文，所以把 ASCII 字母数字挑出来拼，
// 拼不出来（纯中文）就用 c1、c2…（按列表顺序）。重名时补序号。
// 代号只会随名字变化，改了配置名记得重新看一眼 /strm。
func configAliases(cfgs []model.StrmConfig) map[string]string {
	out := make(map[string]string, len(cfgs))
	used := make(map[string]bool, len(tgReserved)+len(cfgs))
	for k := range tgReserved {
		used[k] = true
	}
	for i, c := range cfgs {
		base := cmdSlug(c.Name)
		if base == "" {
			base = fmt.Sprintf("c%d", i+1)
		}
		alias := base
		for n := 2; used[alias]; n++ {
			alias = fmt.Sprintf("%s%d", base, n)
		}
		used[alias] = true
		out[c.ID] = alias
	}
	return out
}

// cmdSlug 把配置名压成命令能用的片段：小写 ASCII 字母数字，最多 12 位。
func cmdSlug(name string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(strings.TrimSpace(name)) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		}
	}
	s := b.String()
	if len(s) > 12 {
		s = s[:12]
	}
	// 纯数字开头能当命令，但可读性太差，加个 c
	if s != "" && s[0] >= '0' && s[0] <= '9' {
		s = "c" + s
	}
	return s
}

// genModeText 是给聊天窗口看的中文说法。
func genModeText(mode GenMode) string {
	return map[GenMode]string{ModeInc: "增量生成", ModeFull: "全量生成", ModeRebuild: "全新生成"}[mode]
}

// TGConfigAliases 给 server 层回显用：配置 id → TG 指令代号。
// 必须和 /help、/strm 里用的那套一致，所以只留这一个出口。
func (a *App) TGConfigAliases() map[string]string {
	return configAliases(a.DB.ListConfigs())
}

// tgConfigList 拼出「配置 → 专属指令」的列表，/help 与 /strm 共用。
func (a *App) tgConfigList(withCount bool) string {
	cfgs := a.DB.ListConfigs()
	if len(cfgs) == 0 {
		return "还没有生成配置"
	}
	aliases := configAliases(cfgs)
	var b strings.Builder
	for _, c := range cfgs {
		alias := aliases[c.ID]
		if withCount {
			fmt.Fprintf(&b, "• %s：%d 个文件（%s）\n", c.Name, a.DB.CountFiles(c.ID), firstNonEmptyStr(c.LastStatus, "未跑过"))
		} else {
			fmt.Fprintf(&b, "• %s\n", c.Name)
		}
		fmt.Fprintf(&b, "  增量 /inc_%s · 全量 /full_%s\n", alias, alias)
	}
	return strings.TrimRight(b.String(), "\n")
}

// tgCleanupList 列出待确认的清理计划；ID 只展示前 10 位，指令按前缀匹配。
func (a *App) tgCleanupList() string {
	list := a.CleanupQueue()
	if len(list) == 0 {
		return "没有待确认的清理计划"
	}
	var b strings.Builder
	for _, x := range list {
		short := x.ID
		if len(short) > 10 {
			short = short[:10]
		}
		fmt.Fprintf(&b, "• %s · %d 个残留 · ID %s\n", x.ConfigName, x.Total, short)
	}
	b.WriteString("确认 /cleanup_ok <ID> · 取消 /cleanup_no <ID>")
	return strings.TrimRight(b.String(), "\n")
}

func (a *App) tgCleanupAct(arg string, approve bool) string {
	prefix := strings.TrimSpace(arg)
	if prefix == "" {
		return "用法：/cleanup_ok <ID> 或 /cleanup_no <ID>，ID 见 /cleanup"
	}
	var matched *model.CleanupConfirmation
	for _, x := range a.CleanupQueue() {
		if strings.HasPrefix(x.ID, prefix) {
			x := x
			matched = &x
			break
		}
	}
	if matched == nil {
		return "没有找到这个 ID 的待确认计划，发 /cleanup 看列表"
	}
	if approve {
		n, err := a.ApproveCleanup(matched.ID)
		if err != nil {
			return err.Error()
		}
		return fmt.Sprintf("已确认，移除 %d 个残留", n)
	}
	if err := a.CancelCleanup(matched.ID); err != nil {
		return err.Error()
	}
	return "已取消该清理计划"
}

// tgHelp 是指令总览。配置多的时候会超 4096 字符，调用方用 sendLong 分段发。
func (a *App) tgHelp() string {
	var b strings.Builder
	b.WriteString("PanShare 指令\n\n")

	b.WriteString("【查看】\n")
	b.WriteString("/status 运行状态\n")
	b.WriteString("/accounts 115 账号\n")
	b.WriteString("/strm 配置、文件数与专属指令\n\n")

	b.WriteString("【生成 strm】\n")
	b.WriteString("/inc <配置|all> 增量（只补新文件，快）\n")
	b.WriteString("/full <配置|all> 全量（对账，清掉已消失的）\n")
	b.WriteString("每条配置都有专属指令，下面「当前配置」里那两行直接用：\n")
	fmt.Fprintf(&b, "%s\n\n", a.tgConfigList(false))

	b.WriteString("【发链接自动生成】\n")
	b.WriteString("按「ed2k」「分享转存」页面里的账号/目录/本地目录设置执行：\n")
	b.WriteString("• 直接发 ed2k:// 链接（多行可一次发多条）\n")
	b.WriteString("• 直接发 115 分享链接（带提取码也行）\n")
	b.WriteString("/ed2k <链接> 显式按 ed2k 处理\n")
	b.WriteString("/share <链接> 显式按分享转存处理\n\n")

	b.WriteString("【其它】\n")
	b.WriteString("/find <关键词> 搜索索引\n")
	b.WriteString("/cleanup 查看待确认清理计划\n")
	b.WriteString("/cleanup_ok <ID> /cleanup_no <ID> 确认或取消清理\n")
	b.WriteString("/link 立即跑一次联动删除对账")
	return b.String()
}

// tgGen 按名字 / 代号 / all 挑配置并启动生成。
func (a *App) tgGen(arg string, mode GenMode) string {
	cfgs := a.DB.ListConfigs()
	if len(cfgs) == 0 {
		return "还没有生成配置，先去网页上建一条"
	}
	aliases := configAliases(cfgs)
	want := strings.TrimSpace(arg)
	all := want == "" || strings.EqualFold(want, "all")

	// 先按 ID / 代号精确匹配，撞上了就只跑这些；一个都没撞上才退回名字包含。
	// 别把两种匹配并在一起：全量会删掉「已消失」的 strm，多选中一条的代价
	// 比少选中一条大得多。
	picked := make([]model.StrmConfig, 0, len(cfgs))
	if all {
		picked = append(picked, cfgs...)
	} else {
		for _, c := range cfgs {
			if c.ID == want || strings.EqualFold(aliases[c.ID], want) {
				picked = append(picked, c)
			}
		}
		if len(picked) == 0 {
			for _, c := range cfgs {
				if strings.Contains(strings.ToLower(c.Name), strings.ToLower(want)) {
					picked = append(picked, c)
				}
			}
		}
	}
	if len(picked) == 0 {
		return fmt.Sprintf("没有匹配「%s」的配置，发 /strm 看可用指令", want)
	}
	return a.tgStart(picked, mode)
}

// tgGenByAlias 只认代号，供 /inc_<代号>、/full_<代号> 用。
//
// 这里故意不做「包含」匹配：用户敲 /inc_movie 时心里想的是一条配置，
// 按包含去猜会把 movies2、movie-hd 一起点了（全量还会顺带删文件）。
func (a *App) tgGenByAlias(alias string, mode GenMode) string {
	cfgs := a.DB.ListConfigs()
	aliases := configAliases(cfgs)
	for _, c := range cfgs {
		if strings.EqualFold(aliases[c.ID], alias) {
			return a.tgStart([]model.StrmConfig{c}, mode)
		}
	}
	return fmt.Sprintf("没有代号为 %s 的配置，发 /strm 看可用指令", alias)
}

// tgStart 真正启动生成并拼回复。
func (a *App) tgStart(picked []model.StrmConfig, mode GenMode) string {
	started := make([]string, 0, len(picked))
	var problems []string
	for _, c := range picked {
		ok, err := a.RunStrm(c.ID, mode)
		switch {
		case ok:
			started = append(started, c.Name)
		case err != nil:
			problems = append(problems, fmt.Sprintf("%s：%v", c.Name, err))
		default:
			problems = append(problems, c.Name+"：正在跑，跳过")
		}
	}

	var b strings.Builder
	if len(started) > 0 {
		fmt.Fprintf(&b, "已启动 %d 条配置的%s：\n", len(started), genModeText(mode))
		for _, n := range started {
			fmt.Fprintf(&b, "• %s\n", n)
		}
		b.WriteString("跑完会推一条结果通知。")
	}
	if len(problems) > 0 {
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		b.WriteString("没启动的：\n")
		for _, p := range problems {
			fmt.Fprintf(&b, "• %s\n", p)
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// ---------------------------------------------------------------- 命令菜单

// tgCommandTable 是注册给 Telegram 的命令菜单（聊天框里敲 / 会弹出来）。
//
// 除了固定指令，每条生成配置还会注册 inc_<代号> / full_<代号> 两条，
// 用户在手机上不用背名字，选一下就跑了。
// Telegram 限制：command 只能小写字母数字下划线、最长 32；description 3~256；
// 一个 bot 最多 100 条。
func (a *App) tgCommandTable() []map[string]string {
	out := []map[string]string{
		{"command": "help", "description": "全部指令（含每条配置的增量/全量）"},
		{"command": "status", "description": "运行状态：账号、配置、文件数"},
		{"command": "accounts", "description": "115 账号列表"},
		{"command": "strm", "description": "生成配置、文件数与专属指令"},
		{"command": "inc", "description": "增量生成：/inc 配置名 或 /inc all"},
		{"command": "full", "description": "全量生成：/full 配置名 或 /full all"},
		{"command": "ed2k", "description": "添加 ed2k 链接并生成 strm"},
		{"command": "share", "description": "转存 115 分享并生成 strm"},
		{"command": "find", "description": "按关键词搜索索引"},
		{"command": "cleanup", "description": "查看待确认的清理计划"},
		{"command": "cleanup_ok", "description": "确认清理：/cleanup_ok <ID>"},
		{"command": "cleanup_no", "description": "取消清理：/cleanup_no <ID>"},
		{"command": "link", "description": "跑一次联动删除对账"},
	}

	cfgs := a.DB.ListConfigs()
	aliases := configAliases(cfgs)
	for _, c := range cfgs {
		alias := aliases[c.ID]
		if alias == "" {
			continue
		}
		if len(out)+2 > 100 {
			break
		}
		name := trimRunes(c.Name, 40)
		out = append(out,
			map[string]string{"command": "inc_" + alias, "description": "增量：" + name},
			map[string]string{"command": "full_" + alias, "description": "全量：" + name},
		)
	}
	return out
}

// trimRunes 按字符数截断（中文按字算，不按字节），超了补省略号。
func trimRunes(s string, max int) string {
	r := []rune(strings.TrimSpace(s))
	if len(r) <= max {
		return string(r)
	}
	return string(r[:max]) + "…"
}

// ---------------------------------------------------------------- 链接

// 链接字符集：除了空白和中日韩文字，其它都算链接的一部分。
//
// 排除汉字很关键：TG 里常写「https://115.com/s/xxx，这个是分享」，
// 只按空白截断会把「，这个是分享」一起当成链接，ParseShare 直接失败。
const tgLinkChar = `[^\s\p{Han}]+`

// 链接后面常跟着中文标点或括号，直接拿去解析会失败。
const tgLinkTrim = "，。；、）,.;)》】」』]\"'!?！？…"

var (
	reEd2kLink  = regexp.MustCompile(`(?i)ed2k://` + tgLinkChar)
	reShareLink = regexp.MustCompile(`(?i)https?://[^\s\p{Han}]*(?:115\.com|115cdn\.com|anxia\.com)/s/` + tgLinkChar)
)

// extractLinks 从一段话里挑出 ed2k 与 115 分享链接（去重、保序）。
func extractLinks(text string) (ed2ks, shares []string) {
	seen := map[string]bool{}
	push := func(dst *[]string, s string) {
		s = strings.TrimRight(strings.TrimSpace(s), tgLinkTrim)
		if s == "" || seen[s] {
			return
		}
		seen[s] = true
		*dst = append(*dst, s)
	}
	for _, m := range reEd2kLink.FindAllString(text, -1) {
		push(&ed2ks, m)
	}
	for _, m := range reShareLink.FindAllString(text, -1) {
		push(&shares, m)
	}
	return ed2ks, shares
}

// tgHandleLinks 把消息里的链接变成任务。
//
// only 为空 = 自动识别（两种都收）；"ed2k" / "share" = 命令里显式指定只处理一种。
// 什么都没找到时返回空串：普通聊天不该被机器人回一句「未知指令」。
func (a *App) tgHandleLinks(text, only string) string {
	ed2ks, shares := extractLinks(text)
	switch only {
	case "ed2k":
		shares = nil
	case "share":
		ed2ks = nil
	}
	if len(ed2ks) == 0 && len(shares) == 0 {
		switch only {
		case "ed2k":
			return "用法：/ed2k <ed2k 链接>，也可以直接把链接发给我"
		case "share":
			return "用法：/share <115 分享链接>，也可以直接把链接发给我"
		}
		return ""
	}

	s := a.Settings()
	var b strings.Builder

	if len(ed2ks) > 0 {
		tasks, err := a.AddEd2k(Ed2kRequest{URLs: strings.Join(ed2ks, "\n")})
		switch {
		case err != nil:
			fmt.Fprintf(&b, "❌ ed2k 添加失败：%v\n", err)
		case len(tasks) > 0:
			t := tasks[0]
			acc := t.AccountID
			if row, _ := a.DB.GetAccount(t.AccountID); row != nil {
				acc = accountLabel(row)
			}
			fmt.Fprintf(&b, "✅ 已添加 %d 条 ed2k\n", len(tasks))
			fmt.Fprintf(&b, "账号 %s · 目录 %s\n", acc, firstNonEmptyStr(s.Ed2kDirName, s.Ed2kDirCID, "根目录"))
			fmt.Fprintf(&b, "本地 %s\n", t.LocalDir)
			if t.TransferOnAdd {
				b.WriteString("已同时提交离线转存，转完自动补直链。")
			} else {
				b.WriteString("strm 已生成，首次播放时才提交离线转存。")
			}
		}
	}

	for _, link := range shares {
		task, err := a.CreateShare(ShareRequest{
			URL:       link,
			AccountID: s.ShareAccountID,
			DirCID:    s.ShareDirCID,
			DirName:   s.ShareDirName,
			LocalDir:  s.ShareLocalDir,
		})
		if err != nil {
			if b.Len() > 0 {
				b.WriteString("\n")
			}
			fmt.Fprintf(&b, "❌ 分享 %s：%v", shortLink(link), err)
			continue
		}
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		fmt.Fprintf(&b, "✅ 分享已提交转存：%s\n", task.ShareCode)
		b.WriteString("转存完会通知，strm 落到「分享转存」页面设置的本地目录。")
	}
	return strings.TrimRight(b.String(), "\n")
}

// shortLink 只留链接尾巴，聊天窗口里整条 CDN 长链看着累。
func shortLink(link string) string {
	link = strings.TrimRight(link, "/")
	if i := strings.LastIndexAny(link, "/#?"); i >= 0 && len(link)-i > 1 {
		return "…" + link[i:]
	}
	return link
}
