package traffic

import (
	"context"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
)

// TestHistoryAggregatesPerRound 验证聚合曲线按「采样轮次」对齐，
// 而不是按时间戳的整秒分桶。
//
// 这是一个真实缺陷：PollOnce 对所有客户端共用同一个 now，
// 但各客户端 Fetch 并发执行，写入历史的 Timestamp 带各自毫秒。
// 一旦跨过秒边界（如 12:00:00.998 与 12:00:01.002），
// 按 Unix() 分桶就会把同一轮拆成两个点，每个点只含部分客户端的量，
// 曲线上表现为「累积量少了一截」，与顶部卡片对不上。
func TestHistoryAggregatesPerRound(t *testing.T) {
	c1 := &scriptedClient{name: "a"}
	c2 := &scriptedClient{name: "b"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(c1))
	reg.Add(client.NewManaged(c2))
	m := NewMonitor(reg, time.Second, 100)

	ctx := context.Background()

	// 第 1 轮：建立基线
	t0 := time.Now()
	c1.set(1000, 0)
	c2.set(2000, 0)
	m.PollOnce(ctx, t0)

	// 第 2 轮：两个客户端都产生流量。
	// 注意这里用同一个 now，模拟真实情况（PollOnce 传入统一时间）。
	t1 := t0.Add(time.Second)
	c1.set(1000, 5000)
	c2.set(2000, 8000)
	m.PollOnce(ctx, t1)

	agg := m.Aggregate()
	hist := m.History("")

	if len(hist) == 0 {
		t.Fatal("历史不应为空")
	}

	// 最后一个点的周期量应与聚合快照一致
	last := hist[len(hist)-1]
	if last.WindowBytes != agg.WindowBytes {
		t.Errorf("曲线末点周期量 = %d, 聚合快照 = %d（两者应一致）",
			last.WindowBytes, agg.WindowBytes)
	}

	// 关键：不能出现「半截数据」的点
	// 每轮的周期量应单调不减（同一周期内）
	for i := 1; i < len(hist); i++ {
		if hist[i].WindowBytes < hist[i-1].WindowBytes {
			t.Errorf("周期量在曲线中减少了: [%d]=%d -> [%d]=%d",
				i-1, hist[i-1].WindowBytes, i, hist[i].WindowBytes)
		}
	}
}

// TestHistoryDoesNotSplitRoundAcrossSecondBoundary 验证跨秒边界的同一轮
// 不会被拆成两个点。
func TestHistoryDoesNotSplitRoundAcrossSecondBoundary(t *testing.T) {
	c1 := &scriptedClient{name: "a"}
	c2 := &scriptedClient{name: "b"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(c1))
	reg.Add(client.NewManaged(c2))
	m := NewMonitor(reg, time.Second, 100)
	ctx := context.Background()

	base := time.Now()
	c1.set(100, 0)
	c2.set(200, 0)
	m.PollOnce(ctx, base)

	// 关键：让两个客户端的样本落在【秒边界两侧】。
	// 用 5 秒间隔，模拟一轮采样内跨秒的抖动。
	t1 := base.Add(5 * time.Second)
	c1.set(100, 1000)
	c2.set(200, 2000)
	m.PollOnce(ctx, t1)

	hist := m.History("")
	// 两轮采样 -> 应当只有 2 个点（基线 + 第二轮）
	if len(hist) != 2 {
		t.Errorf("历史点数 = %d, 期望 2（每轮一个点）", len(hist))
		for i, s := range hist {
			t.Logf("  [%d] ts=%s win=%d up=%d", i, s.Timestamp.Format("15:04:05.000"), s.WindowBytes, s.UpSpeed)
		}
	}

	// 第二轮的点必须包含【两个客户端】的量
	if len(hist) >= 2 {
		last := hist[len(hist)-1]
		// c1 增量 1000 + c2 增量 2000 = 3000
		if last.WindowBytes != 3000 {
			t.Errorf("末点周期量 = %d, 期望 3000（两个客户端之和）", last.WindowBytes)
		}
		if last.UpSpeed != 300 {
			t.Errorf("末点速率 = %d, 期望 300（100+200）", last.UpSpeed)
		}
	}
}

// TestHistorySingleClientUnaffected 验证单客户端查询不受聚合逻辑影响。
func TestHistorySingleClientUnaffected(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	sc.set(1000, 0)
	m.PollOnce(ctx, t0)
	sc.set(1000, 5000)
	m.PollOnce(ctx, t0.Add(time.Second))

	h := m.History("c1")
	if len(h) != 2 {
		t.Errorf("单客户端历史点数 = %d, 期望 2", len(h))
	}
	if h[len(h)-1].UpSpeed != 1000 {
		t.Errorf("速率 = %d, 期望 1000", h[len(h)-1].UpSpeed)
	}
}
