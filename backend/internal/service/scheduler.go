package service

import (
	"context"
	"log"
	"strings"
	"time"

	"panshare/internal/model"
)

// 后台调度：
//   - 定时巡检（每条配置自己设间隔）
//   - 事件监控（轮询目录指纹，变了才生成，带延时）
//   - 联动删除对账
//   - Telegram 指令轮询、Emby 反代端口、插件自启

const (
	// minWatchInterval 是事件监控轮询间隔的下限：太密会把 115 接口打成限流。
	minWatchInterval = 15 * time.Second
	// defaultWatchInterval 是没配过时的轮询间隔。
	defaultWatchInterval = 60 * time.Second
)

func (a *App) Start(ctx context.Context) {
	go a.TG.Start(ctx)
	// 先让已装插件跟上镜像版本，再拉起（升级容器后不用手动重装）
	a.Plugins.SyncBuiltins()
	go a.Plugins.Autostart()
	go a.Proxy.Start(ctx)
	go a.scheduleLoop(ctx)
}

func (a *App) scheduleLoop(ctx context.Context) {
	// 用上次运行时间去填种子：地图是空的，直接拿零值比较的话
	// 容器每重启一次就会把所有配置立刻跑一遍全量扫描。
	watchAt := map[string]time.Time{} // key 是账号 id：事件监控按账号拉，一轮分发给所有配置
	sched := map[string]time.Time{}   // 上次定时巡检时间
	cronLast := map[string]string{}   // 上次执行 cron 全量的分钟，避免同一分钟重复触发
	fails := map[string]int{}         // 连续失败次数，用于退避
	lastLink := time.Time{}

	seed := func(c *model.StrmConfig) {
		if _, ok := sched[c.ID]; !ok {
			sched[c.ID] = unixOrZero(c.LastRunAt)
		}
	}

	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			cfgs := a.DB.ListConfigs()
			watchByAcc := map[string][]*model.StrmConfig{}
			for i := range cfgs {
				c := &cfgs[i]
				seed(c)
				if c.WatchEnabled {
					watchByAcc[c.AccountID] = append(watchByAcc[c.AccountID], c)
				}
				if a.IsRunning(c.ID) {
					continue
				}
				// 定时巡检
				if c.SchedEnabled && c.SchedMinutes > 0 {
					if now.Sub(sched[c.ID]) >= time.Duration(c.SchedMinutes)*time.Minute {
						sched[c.ID] = now
						log.Printf("定时巡检触发增量：%s（间隔 %d 分钟）", c.Name, c.SchedMinutes)
						_, _ = a.RunStrm(c.ID, ModeInc)
					}
				}
				// 定时全量（5 段 cron）
				if strings.TrimSpace(c.CronFull) != "" {
					minuteKey := now.Format("2006-01-02 15:04")
					if cronLast[c.ID] != minuteKey {
						if expr, err := parseCron(c.CronFull); err == nil && expr.match(now) {
							cronLast[c.ID] = minuteKey
							log.Printf("cron 触发全量：%s（%s）", c.Name, c.CronFull)
							_, _ = a.RunStrm(c.ID, ModeFull)
						}
					}
				}
			}
			for accID, list := range watchByAcc {
				if next, ok := watchAt[accID]; ok && now.Before(next) {
					continue
				}
				interval := time.Duration(a.watchIntervalOf(list[0])) * time.Second
				for _, c := range list[1:] {
					if v := time.Duration(a.watchIntervalOf(c)) * time.Second; v < interval {
						interval = v
					}
				}
				watchAt[accID] = now.Add(interval)
				go a.watchAccount(ctx, accID, list, &fails)
			}
			if a.Settings().DeleteEnabled && now.Sub(lastLink) > 10*time.Minute {
				lastLink = now
				go a.LinkScan(ctx)
			}
		}
	}
}

func (a *App) watchIntervalOf(c *model.StrmConfig) int {
	v := c.WatchInterval
	if v <= 0 {
		v = a.Settings().WatchIntervalSec
	}
	if v <= 0 {
		v = int(defaultWatchInterval / time.Second)
	}
	if min := int(minWatchInterval / time.Second); v < min {
		v = min
	}
	return v
}

// watchAccount 是一轮事件监控：先走 115 生活事件（一个事件处理一个文件），
// 事件接口不可用时退回目录指纹探测。
func (a *App) watchAccount(ctx context.Context, accID string, cfgs []*model.StrmConfig, fails *map[string]int) {
	if ctx.Err() != nil {
		return
	}
	if a.lifeWatch(ctx, accID, cfgs) {
		if fails != nil {
			delete(*fails, accID)
		}
		return
	}
	for _, c := range cfgs {
		a.watchProbe(ctx, c, fails)
	}
}

// watchProbe 探一次目录指纹：没变化就直接返回，变了才真正跑增量。
// 只在事件接口不可用时使用。
func (a *App) watchProbe(ctx context.Context, c *model.StrmConfig, fails *map[string]int) {
	if !c.WatchEnabled || ctx.Err() != nil {
		return
	}
	sig, err := a.StrmSignature(c)
	_ = a.DB.UpdateConfig(c.ID, map[string]any{"last_check_at": time.Now().Unix()})
	if err != nil {
		a.bumpFail(fails, c.ID, c.Name, "扫描目录失败", err)
		return
	}
	if sig == c.LastSig {
		return
	}
	if a.IsRunning(c.ID) {
		// 有别的任务正在跑，让它跑完把指纹写进库
		return
	}
	delay := c.WatchDelaySec
	if delay <= 0 {
		delay = a.Settings().WatchDelaySec
	}
	log.Printf("事件监控：%s 目录有变化，等待 %d 秒后增量生成", c.Name, delay)
	if delay > 0 {
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(delay) * time.Second):
		}
	}
	if !a.tryBegin(c.ID) {
		return
	}
	defer a.endRun(c.ID)
	_ = a.DB.UpdateConfig(c.ID, map[string]any{"last_status": "running"})
	a.runStrm(c, ModeInc)

	// 失败就别把指纹记成「已同步」以外的状态，下次探测会自动重试；
	// 连续失败按退避拉开间隔，避免每轮都刷事件。
	after, err := a.DB.GetConfig(c.ID)
	if err == nil && after != nil && after.LastStatus == "error" {
		a.bumpFail(fails, c.ID, c.Name, "增量生成失败", nil)
		return
	}
	if fails != nil {
		delete(*fails, c.ID)
	}
}

// bumpFail 记录连续失败，并把下次探测时间往后推（指数退避，封顶 30 分钟）。
func (a *App) bumpFail(fails *map[string]int, id, name, what string, err error) {
	if fails == nil {
		return
	}
	(*fails)[id]++
	n := (*fails)[id]
	wait := time.Duration(1<<uint(minInt(n, 5))) * time.Minute
	if wait > 30*time.Minute {
		wait = 30 * time.Minute
	}
	msg := what
	if err != nil {
		msg += "：" + err.Error()
	}
	msg += "（连续 " + itoa(n) + " 次，暂停监控 " + wait.String() + "）"
	a.Event("warn", "strm", name+" · "+msg, "")
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [8]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

func unixOrZero(sec int64) time.Time {
	if sec <= 0 {
		return time.Time{}
	}
	return time.Unix(sec, 0)
}
