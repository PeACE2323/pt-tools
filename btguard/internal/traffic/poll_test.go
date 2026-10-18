package traffic

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
)

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

// scriptedClient 让测试能精确控制每次 Fetch 上报的速率与累计量。
type scriptedClient struct {
	name string

	mu      sync.Mutex
	upSpeed int64
	upTotal int64
	// selfLimit 是客户端当前自报的限速值。
	selfLimit int64

	fetchErr error
}

func (s *scriptedClient) Name() string { return s.name }
func (s *scriptedClient) Type() string { return "scripted" }
func (s *scriptedClient) URL() string  { return "http://fake" }

func (s *scriptedClient) Fetch(_ context.Context) (client.Stats, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fetchErr != nil {
		return client.Stats{}, s.fetchErr
	}
	return client.Stats{
		Timestamp:      time.Now(),
		UpSpeed:        s.upSpeed,
		UpTotal:        s.upTotal,
		CurrentUpLimit: s.selfLimit,
	}, nil
}

func (s *scriptedClient) SetUploadLimit(_ context.Context, l client.SpeedLimit) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.selfLimit = l.Total
	return nil
}

func (s *scriptedClient) set(upSpeed, upTotal int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.upSpeed, s.upTotal = upSpeed, upTotal
}

func (s *scriptedClient) fail(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.fetchErr = err
}

// newMon 构造一个带单个假客户端的监控器。
func newMon(t *testing.T) (*Monitor, *scriptedClient) {
	t.Helper()
	sc := &scriptedClient{name: "c1"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(sc))
	return NewMonitor(reg, time.Second, 50), sc
}

// windowBytes 取某客户端的周期累计；查不到返回 -1。
func windowBytes(m *Monitor, name string) int64 {
	s, ok := m.Snapshot(name)
	if !ok {
		return -1
	}
	return s.WindowBytes
}

// ---------------------------------------------------------------------------
// 累计量的基本语义
// ---------------------------------------------------------------------------

// TestFirstPollEstablishesBaseline 验证首次采样只建立基线、不计入周期。
//
// 原因：客户端的累计上传量是自其启动以来的总量，
// 首次采样若直接计入，会把「历史流量」算进当前周期，
// 导致策略一启动就误判超限。
func TestFirstPollEstablishesBaseline(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()

	// 客户端已累计上传 100 GiB（历史流量）
	sc.set(0, 100<<30)
	m.PollOnce(ctx, time.Now())

	if got := windowBytes(m, "c1"); got != 0 {
		t.Errorf("首次采样后周期累计 = %d, 期望 0（历史流量不应计入本周期）", got)
	}
}

// TestDeltaAccumulation 验证按客户端自报累计量的差值累加。
//
// 注意测试数据必须【物理自洽】：速率与增量要匹配，
// 因为 monitor 对单次增量有一个「峰值速率 × 间隔」的防御性上限，
// 若构造出「1 秒内以 1024 B/s 传出 5000 字节」这种不可能的数据，
// 会被该上限截断，测试就会误报。
func TestDeltaAccumulation(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	// 速率 1 MiB/s，1 秒内的增量应与之一致
	const rate = 1 << 20
	sc.set(rate, 1000)
	m.PollOnce(ctx, t0) // 建立基线

	// 1 秒后累计量增加 1 MiB（与速率自洽）
	sc.set(rate, 1000+rate)
	m.PollOnce(ctx, t0.Add(time.Second))

	if got := windowBytes(m, "c1"); got != rate {
		t.Errorf("周期累计 = %d, 期望 %d", got, rate)
	}

	// 再过 1 秒，再增加 1 MiB
	sc.set(rate, 1000+2*rate)
	m.PollOnce(ctx, t0.Add(2*time.Second))

	if got := windowBytes(m, "c1"); got != 2*rate {
		t.Errorf("周期累计 = %d, 期望 %d", got, 2*rate)
	}
}

// TestClientRestartFallsBackToIntegration 验证客户端重启（累计量归零）时
// 退化为速率积分，而不是产生一个巨大的负数把累计量冲掉。
//
// 这是真实场景：qBittorrent 重启后 up_info_data 会归零。
func TestClientRestartFallsBackToIntegration(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	const rate = 1 << 20
	sc.set(rate, 0)
	m.PollOnce(ctx, t0)

	sc.set(rate, rate)
	m.PollOnce(ctx, t0.Add(time.Second))
	before := windowBytes(m, "c1")
	if before != rate {
		t.Fatalf("前置条件不成立，周期累计 = %d, 期望 %d", before, rate)
	}

	// 客户端重启：累计量归零，速率不变
	sc.set(rate, 0)
	m.PollOnce(ctx, t0.Add(2*time.Second))

	after := windowBytes(m, "c1")
	if after < before {
		t.Errorf("客户端重启后累计量不应减少: %d -> %d", before, after)
	}
	// 退化路径用速率积分：rate × 1s，允许少量误差
	gained := after - before
	if gained < rate*9/10 || gained > rate*11/10 {
		t.Errorf("重启这一段应约等于 %d 字节（速率积分），实际增加 %d", rate, gained)
	}
}

// TestNoCumulativeUsesIntegration 验证客户端不提供累计量时用速率积分。
func TestNoCumulativeUsesIntegration(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	// upTotal 始终为 0，只有速率
	sc.set(2048, 0)
	m.PollOnce(ctx, t0)
	sc.set(2048, 0)
	m.PollOnce(ctx, t0.Add(time.Second))

	got := windowBytes(m, "c1")
	// 梯形法：平均速率 2048 × 1s = 2048
	if got < 1800 || got > 2300 {
		t.Errorf("速率积分结果 = %d, 期望约 2048", got)
	}
}

// TestDeltaGuardAgainstSpike 验证单次采样增量不会异常放大。
//
// 防御场景：客户端时钟跳变、或累计量字段被第三方修改，
// 导致差值异常巨大。若不设上限，一次就能让周期累计爆表并误触发限速。
func TestDeltaGuardAgainstSpike(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	sc.set(100, 1000)
	m.PollOnce(ctx, t0)

	// 异常巨大的跳变（模拟字段被改坏）
	sc.set(100, 1<<50)
	m.PollOnce(ctx, t0.Add(time.Second))

	got := windowBytes(m, "c1")
	// 上限 = max(前后速率) × (间隔秒数+1) × 2 = 100 × 2 × 2 = 400
	if got > 400 {
		t.Errorf("异常增量未被限制: %d（上限应约 400）", got)
	}
}

// TestFetchErrorKeepsLastSnapshot 验证采集失败时保留上一次快照，
// 且不污染累计量。
func TestFetchErrorKeepsLastSnapshot(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	sc.set(1024, 1000)
	m.PollOnce(ctx, t0)
	sc.set(1024, 5000)
	m.PollOnce(ctx, t0.Add(time.Second))

	before := windowBytes(m, "c1")
	snapBefore, _ := m.Snapshot("c1")

	// 采集失败
	sc.fail(context.DeadlineExceeded)
	m.PollOnce(ctx, t0.Add(2*time.Second))

	after := windowBytes(m, "c1")
	if after != before {
		t.Errorf("采集失败不应改变累计量: %d -> %d", before, after)
	}
	snap, ok := m.Snapshot("c1")
	if !ok {
		t.Fatal("失败后应保留快照")
	}
	if snap.Healthy {
		t.Error("失败后 Healthy 应为 false")
	}
	if snap.LastError == "" {
		t.Error("失败后应记录错误信息")
	}
	// 速率应保留上一次的值，便于界面显示最后一次已知状态
	if snap.UpSpeed != snapBefore.UpSpeed {
		t.Error("失败后应保留上一次的速率")
	}
}

// ---------------------------------------------------------------------------
// 周期重置
// ---------------------------------------------------------------------------

// TestResetWindowZeroesAccumulation 验证周期重置会清零周期量，
// 但保留客户端累计基线（否则重置后会漏记或重记）。
func TestResetWindowZeroesAccumulation(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	const rate = 1 << 20
	sc.set(rate, 0)
	m.PollOnce(ctx, t0)
	// 2 秒内累计 2 MiB（与速率自洽）
	sc.set(rate, 2*rate)
	m.PollOnce(ctx, t0.Add(2*time.Second))

	if got := windowBytes(m, "c1"); got != 2*rate {
		t.Fatalf("前置条件不成立: %d", got)
	}

	m.ResetWindow([]string{"c1"})

	if got := windowBytes(m, "c1"); got != 0 {
		t.Errorf("重置后周期累计 = %d, 期望 0", got)
	}

	// 关键：重置后继续累计时，不应把重置前的差值重新算进来
	sc.set(rate, 3*rate)
	m.PollOnce(ctx, t0.Add(3*time.Second))

	if got := windowBytes(m, "c1"); got != rate {
		t.Errorf("重置后累计 = %d, 期望 %d（只算增量 3MiB-2MiB）", got, rate)
	}
}

// ---------------------------------------------------------------------------
// 聚合与历史
// ---------------------------------------------------------------------------

// TestAggregateSumsAllClients 验证多客户端聚合。
func TestAggregateSumsAllClients(t *testing.T) {
	c1 := &scriptedClient{name: "a"}
	c2 := &scriptedClient{name: "b"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(c1))
	reg.Add(client.NewManaged(c2))
	m := NewMonitor(reg, time.Second, 50)
	ctx := context.Background()
	t0 := time.Now()

	c1.set(1000, 100)
	c2.set(2000, 200)
	m.PollOnce(ctx, t0)
	c1.set(1000, 1100) // +1000
	c2.set(2000, 3200) // +3000
	m.PollOnce(ctx, t0.Add(time.Second))

	agg := m.Aggregate()
	if agg.UpSpeed != 3000 {
		t.Errorf("聚合速率 = %d, 期望 3000", agg.UpSpeed)
	}
	if agg.WindowBytes != 4000 {
		t.Errorf("聚合周期量 = %d, 期望 4000", agg.WindowBytes)
	}
}

// TestHistoryBounded 验证历史点数不超过配置上限（防止内存无限增长）。
func TestHistoryBounded(t *testing.T) {
	sc := &scriptedClient{name: "c1"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(sc))
	const cap = 10
	m := NewMonitor(reg, time.Second, cap)
	ctx := context.Background()
	t0 := time.Now()

	for i := 0; i < 50; i++ {
		sc.set(int64(i), int64(i*100))
		m.PollOnce(ctx, t0.Add(time.Duration(i)*time.Second))
	}

	h := m.History("c1")
	if len(h) > cap {
		t.Errorf("历史点数 = %d, 不应超过上限 %d", len(h), cap)
	}
	if len(h) == 0 {
		t.Error("历史不应为空")
	}
}

// TestAdoptStateFromPreservesCounters 验证热重载迁移会保留累计量。
func TestAdoptStateFromPreservesCounters(t *testing.T) {
	sc := &scriptedClient{name: "c1"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(sc))
	old := NewMonitor(reg, time.Second, 50)
	ctx := context.Background()
	t0 := time.Now()

	const rate = 1 << 20
	sc.set(rate, 0)
	old.PollOnce(ctx, t0)
	sc.set(rate, rate)
	old.PollOnce(ctx, t0.Add(time.Second))

	before := windowBytes(old, "c1")
	if before != rate {
		t.Fatalf("前置条件不成立: %d", before)
	}

	// 模拟热重载：新建监控器并继承状态
	next := NewMonitor(reg, time.Second, 50)
	next.AdoptStateFrom(old)

	if got := windowBytes(next, "c1"); got != before {
		t.Errorf("迁移后周期累计 = %d, 期望 %d（热重载不应清零）", got, before)
	}

	// 继承后继续累计应正确衔接
	sc.set(rate, 2*rate)
	next.PollOnce(ctx, t0.Add(2*time.Second))
	if got := windowBytes(next, "c1"); got != 2*rate {
		t.Errorf("迁移后继续累计 = %d, 期望 %d", got, 2*rate)
	}
}

// TestConcurrentAccessIsRaceFree 验证并发采样与读取不会 panic 或数据竞争。
func TestConcurrentAccessIsRaceFree(t *testing.T) {
	sc := &scriptedClient{name: "c1"}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(sc))
	m := NewMonitor(reg, time.Second, 100)
	ctx := context.Background()
	t0 := time.Now()

	var wg sync.WaitGroup
	stop := make(chan struct{})

	// 写入方
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			sc.set(int64(i*10), int64(i*1000))
			m.PollOnce(ctx, t0.Add(time.Duration(i)*time.Millisecond))
		}
	}()

	// 读取方
	for k := 0; k < 3; k++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				_ = m.Aggregate()
				_, _ = m.Snapshot("c1")
				_ = m.AllSnapshots()
				_ = m.History("c1")
				_ = m.Stats()
			}
		}()
	}

	time.Sleep(150 * time.Millisecond)
	close(stop)
	wg.Wait()
}

// TestStatsReflectsPollCount 验证运行指标正确累加。
func TestStatsReflectsPollCount(t *testing.T) {
	m, sc := newMon(t)
	ctx := context.Background()
	t0 := time.Now()

	sc.set(1, 1)
	m.PollOnce(ctx, t0)
	m.PollOnce(ctx, t0.Add(time.Second))
	m.PollOnce(ctx, t0.Add(2*time.Second))

	st := m.Stats()
	if st["poll_count"].(int64) != 3 {
		t.Errorf("poll_count = %v, 期望 3", st["poll_count"])
	}

	// 制造一次失败
	sc.fail(context.DeadlineExceeded)
	m.PollOnce(ctx, t0.Add(3*time.Second))

	st = m.Stats()
	if st["poll_errors"].(int64) != 1 {
		t.Errorf("poll_errors = %v, 期望 1", st["poll_errors"])
	}
}
