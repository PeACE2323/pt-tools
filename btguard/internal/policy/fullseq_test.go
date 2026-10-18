package policy

import (
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// TestAccumulateAcrossMidnightThenRoll 完整走一遍用户遇到的时序：
//  1. 进程在【昨天】启动
//  2. 一整天持续上传，累计到 2 TB
//  3. 时间跨过零点
//  4. 检查界面读到的「周期累计」
func TestAccumulateAcrossMidnightThenRoll(t *testing.T) {
	pc := basePolicy()
	pc.Align = "day"
	pc.Window = config.Duration(24 * time.Hour)
	pc.ThresholdTotal = config.Bytes(1 << 30) // 1 GiB，与用户一致
	pc.LimitTotal = config.Bitrate(102400)
	pc.Cooldown = config.Duration(10 * time.Minute)

	h := newHarness(t, pc)

	// --- 第 1 步：进程在昨天启动 ---
	yesterday := time.Now().AddDate(0, 0, -1)
	h.engine.mu.Lock()
	h.engine.startedAt = yesterday
	h.engine.mu.Unlock()

	// --- 第 2 步：持续上传 ---
	h.poll()
	h.evaluate()

	// 分多轮累积，模拟真实采样（用 accumulateInSteps 保证物理自洽）
	t0 := time.Now()
	now := accumulateInSteps(h.mon, h.cli, t0, 2<<30, 1<<20) // 2 GiB
	_ = now
	h.evaluate()

	before := h.mon.Aggregate().WindowBytes
	t.Logf("第 1 天累计: %d 字节 (%.2f GiB)", before, float64(before)/(1<<30))
	if before == 0 {
		t.Fatal("前置条件：应已累积")
	}

	// 策略应已触发（超过 1 GiB）
	h.engine.mu.Lock()
	limited := h.engine.policies[0].limited
	limAt := h.engine.policies[0].limitedAt
	h.engine.mu.Unlock()
	t.Logf("策略已限速: %v（触发于 %s）", limited, limAt.Format("15:04:05"))

	// --- 第 3 步：跨过零点 ---
	// 把窗口终点与触发时间都推到过去，模拟「已是第二天」
	h.engine.mu.Lock()
	h.engine.policies[0].windowStart = time.Now().AddDate(0, 0, -1).Truncate(24 * time.Hour)
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Minute)
	h.engine.policies[0].limitedAt = time.Now().Add(-2 * time.Hour) // 冷却期已过
	h.engine.mu.Unlock()

	t.Log("--- 跨过零点 ---")
	h.evaluate()

	// --- 第 4 步：检查 ---
	after := h.mon.Aggregate().WindowBytes
	t.Logf("第 2 天周期累计: %d 字节 (%.2f GiB)", after, float64(after)/(1<<30))

	h.engine.mu.Lock()
	stillLimited := h.engine.policies[0].limited
	end := h.engine.policies[0].windowEnd
	h.engine.mu.Unlock()
	t.Logf("仍处于限速: %v，新窗口终点: %s", stillLimited, end.Format("01-02 15:04"))

	if after != 0 {
		t.Errorf("新周期累计应清零，实际 %d —— 界面会显示 %.2f GiB 的历史量",
			after, float64(after)/(1<<30))
	}
	if !end.After(time.Now()) {
		t.Errorf("窗口终点应推进到未来，实际 %s", end.Format("01-02 15:04"))
	}
}
