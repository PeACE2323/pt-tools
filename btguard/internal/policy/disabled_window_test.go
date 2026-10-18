package policy

import (
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// TestDisabledPolicyStillResetsWindow 验证【禁用】的策略也会滚动周期。
//
// 这是一个真实的界面缺陷：
// 总览页的「周期累计」读的是 monitor 的计数器，
// 而计数器的清零只发生在 rollWindow 里；
// 若策略被禁用，Evaluate 会直接 continue，从不调用 rollWindow，
// 于是计数器永远不清零 —— 用户会看到「周期累计」一直停在
// 昨天甚至更早的历史总量（例如 2.03 TiB），
// 而实际上那个数字和当前周期毫无关系。
func TestDisabledPolicyStillResetsWindow(t *testing.T) {
	no := false
	pc := basePolicy()
	pc.Enabled = &no // 关键：禁用
	pc.Align = "day"
	pc.Window = config.Duration(24 * time.Hour)

	h := newHarness(t, pc)

	// 先累积一些流量
	h.poll()
	h.cli.addBytes(5000)
	h.poll()

	before := h.mon.Aggregate().WindowBytes
	t.Logf("滚动前周期累计: %d", before)
	if before == 0 {
		t.Fatal("前置条件：应有累计量")
	}

	// 把窗口推到过去，模拟「已跨过零点」
	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Minute)
	h.engine.mu.Unlock()

	h.poll()
	h.evaluate()

	after := h.mon.Aggregate().WindowBytes
	t.Logf("滚动后周期累计: %d", after)

	if after != 0 {
		t.Errorf("跨过周期后累计量应清零，实际 %d（禁用策略也必须滚动周期，"+
			"否则界面会一直显示历史总量）", after)
	}
}
