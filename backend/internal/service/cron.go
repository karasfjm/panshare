package service

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// 只支持 5 段 cron（分 时 日 月 周），够用于「每天几点跑一次全量」。
// 支持 *、?、逗号列表、连字符范围、*/n 与 a-b/n。
type cronExpr struct {
	minute map[int]bool
	hour   map[int]bool
	dom    map[int]bool
	month  map[int]bool
	dow    map[int]bool
}

func parseCronField(raw string, lo, hi int) (map[int]bool, error) {
	out := map[int]bool{}
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "*" || raw == "?" {
		for i := lo; i <= hi; i++ {
			out[i] = true
		}
		return out, nil
	}
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		base, stepText := part, ""
		if i := strings.Index(part, "/"); i >= 0 {
			base, stepText = part[:i], part[i+1:]
		}
		step := 1
		if stepText != "" {
			n, err := strconv.Atoi(stepText)
			if err != nil || n <= 0 {
				return nil, fmt.Errorf("cron 步长不对: %s", part)
			}
			step = n
		}
		start, end := 0, 0
		if base == "*" || base == "?" {
			start, end = lo, hi
		} else if strings.Contains(base, "-") {
			bounds := strings.SplitN(base, "-", 2)
			a, err1 := strconv.Atoi(strings.TrimSpace(bounds[0]))
			b, err2 := strconv.Atoi(strings.TrimSpace(bounds[1]))
			if err1 != nil || err2 != nil {
				return nil, fmt.Errorf("cron 范围不对: %s", part)
			}
			start, end = a, b
		} else {
			n, err := strconv.Atoi(base)
			if err != nil {
				return nil, fmt.Errorf("cron 数值不对: %s", part)
			}
			start, end = n, n
		}
		if start < lo || end > hi || start > end {
			return nil, fmt.Errorf("cron 数值越界: %s", part)
		}
		if stepText == "" && start == end {
			out[start] = true
			continue
		}
		for i := start; i <= end; i += step {
			out[i] = true
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("cron 字段为空")
	}
	return out, nil
}

func parseCron(raw string) (*cronExpr, error) {
	fields := strings.Fields(strings.TrimSpace(raw))
	if len(fields) != 5 {
		return nil, fmt.Errorf("cron 需要 5 段（分 时 日 月 周）")
	}
	e := &cronExpr{}
	var err error
	if e.minute, err = parseCronField(fields[0], 0, 59); err != nil {
		return nil, err
	}
	if e.hour, err = parseCronField(fields[1], 0, 23); err != nil {
		return nil, err
	}
	if e.dom, err = parseCronField(fields[2], 1, 31); err != nil {
		return nil, err
	}
	if e.month, err = parseCronField(fields[3], 1, 12); err != nil {
		return nil, err
	}
	if e.dow, err = parseCronField(fields[4], 0, 7); err != nil {
		return nil, err
	}
	if e.dow[7] {
		e.dow[0] = true
		delete(e.dow, 7)
	}
	return e, nil
}

func (e *cronExpr) match(t time.Time) bool {
	// 标准 cron：日和周都限定时取「或」；只有一个限定时按那个字段算。
	domAny := len(e.dom) == 31
	dowAny := len(e.dow) == 7
	domOK := e.dom[t.Day()]
	dowOK := e.dow[int(t.Weekday())]
	dayOK := domOK && dowOK
	if !domAny && !dowAny {
		dayOK = domOK || dowOK
	}
	return e.minute[t.Minute()] && e.hour[t.Hour()] && e.month[int(t.Month())] && dayOK
}
