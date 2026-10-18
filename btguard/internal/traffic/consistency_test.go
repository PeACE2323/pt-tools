package traffic

import (
	"context"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
)

// TestHistoryMatchesAggregate 验证「曲线末点」与「聚合快照」始终一致。
//
// 用户报告：顶部卡片显示 859.27 MiB，而累计曲线峰值 988.16 MiB，
// 两者都声称是「本周期累计上传」，却对不上。
//
// 这是同一个量在两个地方算出不同结果 —— 必须一致。
func TestHistoryMatchesAggregate(t *testing.T) {
	c1 := &scriptedClient{name: "a"}
	c2 := &scriptedClient{name: "b"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(c1))
	reg.Add(client.NewManaged(c2))
	m := NewMonitor(reg, time.Second, 100)
	ctx := context.Background()

	t0 := time.Now()
	c1.set(1000, 0)
	c2.set(2000, 0)
	m.PollOnce(ctx, t0)

	// 多轮累积
	for i := 1; i <= 5; i++ {
		ti := t0.Add(time.Duration(i) * time.Second)
		c1.set(1000, int64(i*1000))
		c2.set(2000, int64(i*3000))
		m.PollOnce(ctx, ti)

		agg := m.Aggregate()
		h := m.History("")
		last := h[len(h)-1]

		if last.WindowBytes != agg.WindowBytes {
			t.Errorf("第 %d 轮：曲线末点 = %d, 聚合快照 = %d",
				i, last.WindowBytes, agg.WindowBytes)
		}
		if last.UpSpeed != agg.UpSpeed {
			t.Errorf("第 %d 轮：曲线末点速率 = %d, 聚合 = %d",
				i, last.UpSpeed, agg.UpSpeed)
		}
	}

	// 曲线的最大周期量不应超过当前聚合值（同一周期内单调递增）
	h := m.History("")
	agg := m.Aggregate()
	var maxWin int64
	for _, s := range h {
		if s.WindowBytes > maxWin {
			maxWin = s.WindowBytes
		}
	}
	if maxWin > agg.WindowBytes {
		t.Errorf("曲线峰值 %d 超过当前聚合值 %d —— "+
			"同一周期内累计量不该超过当前值", maxWin, agg.WindowBytes)
	}
}

// TestHistoryAfterResetDropsOldPoints 验证周期重置后旧周期的大值
// 不会残留在曲线里，导致「峰值远大于当前值」。
func TestHistoryAfterResetDropsOldPoints(t *testing.T) {
	c1 := &scriptedClient{name: "a"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(c1))
	m := NewMonitor(reg, time.Second, 100)
	ctx := context.Background()

	t0 := time.Now()
	c1.set(1000, 0)
	m.PollOnce(ctx, t0)

	// 累积到一个较大的值
	c1.set(1000, 10000)
	m.PollOnce(ctx, t0.Add(time.Second))
	c1.set(1000, 20000)
	m.PollOnce(ctx, t0.Add(2*time.Second))

	aggBefore := m.Aggregate().WindowBytes
	if aggBefore == 0 {
		t.Fatal("前置条件：应有累计量")
	}

	// 模拟周期滚动：重置累计
	m.ResetWindow([]string{"a"})
	c1.set(1000, 21000) // 客户端累计量继续增长
	m.PollOnce(ctx, t0.Add(3*time.Second))

	h := m.History("")
	var maxWin int64
	for _, s := range h {
		if s.WindowBytes > maxWin {
			maxWin = s.WindowBytes
		}
	}
	aggAfter := m.Aggregate().WindowBytes

	t.Logf("重置前聚合 = %d, 重置后聚合 = %d, 曲线峰值 = %d", aggBefore, aggAfter, maxWin)

	if maxWin > aggAfter {
		t.Errorf("曲线峰值 %d 大于重置后的当前值 %d。\n"+
			"这会让用户在曲线上看到远高于当前值的「累计峰值」，产生困惑。\n"+
			"原因：历史里残留了上一个周期的点。", maxWin, aggAfter)
	}
}
