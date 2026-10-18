package policy

import (
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// TestResumeLimitCustomSpeed 验证「解除限速后回到自定义速度」。
func TestResumeLimitCustomSpeed(t *testing.T) {
	pc := basePolicy()
	pc.ResumeLimitTotal = config.Bitrate(5 << 20) // 周期结束后回到 5 MiB/s
	h := newHarness(t, pc)

	// 触发限速
	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()
	if h.cli.limit() != 100 {
		t.Fatalf("应限速到 100，实际 %d", h.cli.limit())
	}

	// 周期滚动 -> 应恢复到 5 MiB/s，而不是 0
	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 5<<20 {
		t.Errorf("周期结束后应恢复到 5 MiB/s (%d)，实际 %d", 5<<20, got)
	}
}

// TestResumeWithoutCustomFallsBackToBaseline 验证未配置时仍回到原始限速。
func TestResumeWithoutCustomFallsBackToBaseline(t *testing.T) {
	h := newHarness(t, basePolicy())

	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 0 {
		t.Errorf("未配置自定义恢复速度时应回到 0（原始不限速），实际 %d", got)
	}
}

// TestResumeExplicitZero 验证显式配 0 表示不限速。
func TestResumeExplicitZero(t *testing.T) {
	pc := basePolicy()
	pc.ResumeLimitTotal = 0 // 显式不限速
	h := newHarness(t, pc)

	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 0 {
		t.Errorf("显式 0 应表示不限速，实际 %d", got)
	}
	if pc.HasResumeLimit() {
		t.Error("全 0 时 HasResumeLimit 应为 false（视作未配置）")
	}
}
