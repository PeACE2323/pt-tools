package policy

import (
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// TestDailyWindowRollsAtMidnight 验证 align=day 时周期在本地零点滚动。
//
// 场景：现在是 00:16，周期应在 00:00 已经滚动过，
// 累计量应从 0 重新开始。若没有滚动，界面会一直显示昨天的量。
func TestDailyWindowRollsAtMidnight(t *testing.T) {
	loc := time.Local
	pc := basePolicy()
	pc.Align = "day"
	pc.Window = config.Duration(24 * time.Hour)

	h := newHarness(t, pc)

	// 手动把窗口设为「昨天」，然后按真实时间求值
	now := time.Now()
	yesterday := now.Add(-24 * time.Hour)
	h.engine.mu.Lock()
	h.engine.policies[0].windowStart = yesterday
	h.engine.policies[0].windowEnd = time.Date(
		yesterday.Year(), yesterday.Month(), yesterday.Day()+1, 0, 0, 0, 0, loc)
	h.engine.mu.Unlock()

	t.Logf("当前时间    : %s", now.Format("2006-01-02 15:04:05"))
	t.Logf("原窗口终点  : %s", h.engine.policies[0].windowEnd.Format("2006-01-02 15:04:05"))

	h.poll()
	h.evaluate()

	eng := h.engine
	eng.mu.Lock()
	end := eng.policies[0].windowEnd
	start := eng.policies[0].windowStart
	eng.mu.Unlock()

	t.Logf("新窗口      : %s ~ %s", start.Format("01-02 15:04:05"), end.Format("01-02 15:04:05"))

	if !end.After(now) {
		t.Errorf("周期终点应已推进到当前时刻之后，实际 %s", end.Format("2006-01-02 15:04:05"))
	}
	// 窗口应覆盖当前时刻
	if now.Before(start) || !now.Before(end) {
		t.Errorf("当前时刻 %s 不在窗口 %s ~ %s 内",
			now.Format("15:04:05"), start.Format("15:04:05"), end.Format("15:04:05"))
	}
}
