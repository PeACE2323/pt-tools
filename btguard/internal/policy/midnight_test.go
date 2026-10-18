package policy

import (
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// TestWindowRollsWhenProcessRunsAcrossMidnight 模拟「进程一直在跑、跨过零点」。
//
// 这是用户实际遇到的情况：容器没重启，但日期从 10-16 变成了 10-17，
// 周期累计却仍是昨天的 2.06 TB。
func TestWindowRollsWhenProcessRunsAcrossMidnight(t *testing.T) {
	pc := basePolicy()
	pc.Align = "day"
	pc.Window = config.Duration(24 * time.Hour)
	pc.ThresholdTotal = config.Bytes(1 << 30)

	h := newHarness(t, pc)

	// 关键：把 startedAt 设为【昨天】，让引擎以昨天为锚点
	yesterday := time.Now().AddDate(0, 0, -1)
	h.engine.mu.Lock()
	h.engine.startedAt = yesterday
	h.engine.mu.Unlock()

	// 启动后第一次 Evaluate —— 窗口按「昨天」建立
	h.poll()
	h.evaluate()

	h.engine.mu.Lock()
	start1 := h.engine.policies[0].windowStart
	end1 := h.engine.policies[0].windowEnd
	h.engine.mu.Unlock()
	t.Logf("首次建立窗口: %s -> %s",
		start1.Format("01-02 15:04:05"), end1.Format("01-02 15:04:05"))

	// 累积一些流量
	h.cli.addBytes(1 << 20)
	h.poll()
	before := h.mon.Aggregate().WindowBytes
	t.Logf("累积后周期量: %d", before)

	// 现在把窗口终点推到过去，模拟「时间已经越过零点」
	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()

	h.evaluate()
	after := h.mon.Aggregate().WindowBytes
	t.Logf("跨过零点后周期量: %d", after)

	if after != 0 {
		t.Errorf("跨过零点后应清零，实际 %d —— 这会导致界面一直显示昨天的总量", after)
	}
}

// TestWindowEndNotPastAfterRoll 验证滚动后窗口终点必须落在未来。
//
// 若终点仍在过去，下一次 Evaluate 会再次滚动、反复清零，
// 或被误判为「没到期」而永不滚动 —— 两种都会让统计失真。
func TestWindowEndNotPastAfterRoll(t *testing.T) {
	pc := basePolicy()
	pc.Align = "day"
	pc.Window = config.Duration(24 * time.Hour)
	h := newHarness(t, pc)

	// 人为把终点设成很久以前（模拟进程休眠/长时间未求值）
	h.engine.mu.Lock()
	h.engine.policies[0].windowStart = time.Now().AddDate(0, 0, -30)
	h.engine.policies[0].windowEnd = time.Now().AddDate(0, 0, -29)
	h.engine.startedAt = time.Now().AddDate(0, 0, -30)
	h.engine.mu.Unlock()

	h.poll()
	h.evaluate()

	h.engine.mu.Lock()
	end := h.engine.policies[0].windowEnd
	start := h.engine.policies[0].windowStart
	h.engine.mu.Unlock()

	now := time.Now()
	t.Logf("滚动后窗口: %s -> %s (now=%s)",
		start.Format("01-02 15:04:05"), end.Format("01-02 15:04:05"), now.Format("01-02 15:04:05"))

	if !end.After(now) {
		t.Errorf("滚动后终点 %s 应晚于当前时刻 %s", end.Format("15:04:05"), now.Format("15:04:05"))
	}
	if now.Before(start) {
		t.Errorf("当前时刻应落在窗口内")
	}
}

// TestRepeatedEvaluateDoesNotClearRepeatedly 验证反复求值不会反复清零。
func TestRepeatedEvaluateDoesNotClearRepeatedly(t *testing.T) {
	pc := basePolicy()
	pc.Align = "day"
	pc.Window = config.Duration(24 * time.Hour)
	h := newHarness(t, pc)

	h.poll()
	h.evaluate()
	// 同一时刻连续求值多次
	for i := 0; i < 5; i++ {
		h.evaluate()
	}

	h.cli.addBytes(1 << 20)
	h.poll()
	h.evaluate()

	got := h.mon.Aggregate().WindowBytes
	t.Logf("连续求值后周期量: %d", got)
	if got == 0 {
		t.Error("同一周期内反复求值不应清零累计量")
	}
}
