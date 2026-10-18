package policy

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
)

// ---------------------------------------------------------------------------
// 多策略作用于同一客户端
// ---------------------------------------------------------------------------

// accumulateInSteps 让假客户端按真实节奏增长累计量。
//
// 为什么需要：monitor 对单次采样的增量有一个防御上限
//
//	maxDelta = max(前后速率) × (间隔秒数 + 1) × 2
//
// 若一次性把 upTotal 加很多、而两次采样间隔几乎为 0，增量会被截断
// （以 1024 B/s 报出却声称 0 秒内传了 22KB，物理上不可能，截断是对的）。
// 因此测试里必须让「速率 × 时间」与「累计量增量」自洽，
// 否则会误以为产品有 bug。
func accumulateInSteps(m *traffic.Monitor, fc *fakeClient, base time.Time, total, rate int64) time.Time {
	if rate <= 0 {
		rate = 1024
	}
	now := base
	remaining := total
	for remaining > 0 {
		step := rate // 每步最多 1 秒的量
		if remaining < step {
			step = remaining
		}
		fc.addBytes(step)
		now = now.Add(time.Second)
		m.PollOnce(context.Background(), now)
		remaining -= step
	}
	return now
}

// TestMultiplePoliciesOnSameClient 验证多条策略同时作用于同一客户端时的行为。
//
// README 明确承诺：「同一客户端可以被多条策略同时作用，
// 此时以『后触发的那条』下发的限速值为准」。
// 这个承诺此前没有任何测试验证。
func TestMultiplePoliciesOnSameClient(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	notif := &recordingNotifier{}
	eng := NewEngine(reg, mon, notif, func(string, string, ...any) {}, time.UTC)

	yes := true
	// 两条策略都作用于同一客户端，阈值不同、限速值也不同
	eng.Load([]config.PolicyConfig{
		{
			Name: "loose", Enabled: &yes, Clients: []string{"*"},
			Window: config.Duration(time.Hour), Align: "rolling",
			ThresholdTotal: config.Bytes(10000), LimitTotal: config.Bitrate(500),
			ResumeOnWindowReset: &yes,
		},
		{
			Name: "strict", Enabled: &yes, Clients: []string{"*"},
			Window: config.Duration(time.Hour), Align: "rolling",
			ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
			ResumeOnWindowReset: &yes,
		},
	})

	// 首次采样建立基线
	t0 := time.Now()
	mon.PollOnce(context.Background(), t0)
	eng.Evaluate(context.Background())

	// 按真实节奏累积到超过 strict 的阈值（1000），但未到 loose（10000）
	t1 := accumulateInSteps(mon, fc, t0, 2000, 1024)
	eng.Evaluate(context.Background())

	states := eng.States()
	byName := map[string]PolicyState{}
	for _, s := range states {
		byName[s.Name] = s
	}
	if !byName["strict"].Limited {
		t.Fatalf("strict 策略（阈值 1000）应已触发，当前累计=%d", byName["strict"].CurrentTotal)
	}
	if byName["loose"].Limited {
		t.Error("loose 策略（阈值 10000）不应触发")
	}
	if got := fc.limit(); got != 100 {
		t.Errorf("客户端限速 = %d, 期望 100（strict 的值）", got)
	}

	// 继续按真实节奏累积到超过 loose 的阈值
	accumulateInSteps(mon, fc, t1, 20000, 1024)
	eng.Evaluate(context.Background())

	states = eng.States()
	limited := 0
	for _, s := range states {
		if s.Limited {
			limited++
		}
	}
	if limited != 2 {
		t.Errorf("两条策略都应处于限速状态，实际 %d 条", limited)
	}

	// 验证 README 的承诺：后触发的 loose 覆盖了 strict
	if got := fc.limit(); got != 500 {
		t.Errorf("客户端限速 = %d, 期望 500（后触发的 loose 覆盖 strict）", got)
	}
}

// ---------------------------------------------------------------------------
// 客户端在周期中途消失
// ---------------------------------------------------------------------------

// TestFixtureClientRemovedMidWindow 验证策略引用的客户端消失时的行为。
//
// 现实场景：用户在 WebUI 上删掉了一个下载器，但策略还引用着它。
// 程序不应崩溃，也不应产生错误事件刷屏。
func TestFixtureClientRemovedMidWindow(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	eng.Load([]config.PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"c"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}})

	mon.PollOnce(context.Background(), time.Now())
	eng.Evaluate(context.Background())

	// 建立新的空注册表（模拟客户端被移除后重建）
	emptyReg := client.NewRegistry()
	emptyMon := traffic.NewMonitor(emptyReg, time.Second, 100)
	eng2 := NewEngine(emptyReg, emptyMon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)
	eng2.Load([]config.PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"c"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}})

	// 不应 panic；状态查询也应正常返回
	eng2.Evaluate(context.Background())
	states := eng2.States()
	if len(states) != 1 {
		t.Fatalf("应有 1 条策略状态，实际 %d", len(states))
	}
	if states[0].Limited {
		t.Error("没有可用客户端时不应触发限速")
	}
	if states[0].CurrentTotal != 0 {
		t.Errorf("无客户端时累计量应为 0，实际 %d", states[0].CurrentTotal)
	}
}

// TestPolicyWithWildcardAfterClientRemoval 验证通配符策略在客户端变化后仍正确。
func TestPolicyWithWildcardAfterClientRemoval(t *testing.T) {
	reg := client.NewRegistry()
	c1 := newFakeClient("a")
	c2 := newFakeClient("b")
	reg.Add(client.NewManaged(c1))
	reg.Add(client.NewManaged(c2))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	eng.Load([]config.PolicyConfig{{
		Name: "all", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}})

	// 按真实节奏累积（速率 1024 B/s，每步 1 秒）
	t0 := time.Now()
	mon.PollOnce(context.Background(), t0)
	c1.addBytes(600)
	c2.addBytes(600) // 合计 1200 > 1000
	mon.PollOnce(context.Background(), t0.Add(time.Second))
	eng.Evaluate(context.Background())

	if !eng.States()[0].Limited {
		t.Error("合计超过阈值时应触发")
	}
	// 两个客户端都应被限速
	if c1.limit() != 100 || c2.limit() != 100 {
		t.Errorf("通配符策略应作用于全部客户端: a=%d b=%d", c1.limit(), c2.limit())
	}
}

// ---------------------------------------------------------------------------
// 极端配置值
// ---------------------------------------------------------------------------

// TestLimitLargerThanActualBandwidth 验证限速值大于实际带宽时不产生异常。
//
// 用户可能填一个很大的限速值（等于「基本不限速」），
// 程序应如实下发，而不是自作主张地夹取。
func TestLimitLargerThanActualBandwidth(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	const huge = int64(1) << 40 // 1 TiB/s，远超任何真实带宽
	eng.Load([]config.PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(huge),
		ResumeOnWindowReset: &yes,
	}})

	t0 := time.Now()
	mon.PollOnce(context.Background(), t0)
	accumulateInSteps(mon, fc, t0, 2000, 1024)
	eng.Evaluate(context.Background())

	if got := fc.limit(); got != huge {
		t.Errorf("应如实下发配置值 %d，实际 %d", huge, got)
	}
}

// TestVeryLongWindow 验证超长周期（30 天）下的边界计算不溢出。
func TestVeryLongWindow(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	eng.Load([]config.PolicyConfig{{
		Name: "monthly", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(30 * 24 * time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(5 * (1 << 40)), LimitTotal: config.Bitrate(1 << 20),
		ResumeOnWindowReset: &yes,
	}})

	mon.PollOnce(context.Background(), time.Now())
	eng.Evaluate(context.Background())

	st := eng.States()[0]
	if st.WindowEnd.Before(st.WindowStart) {
		t.Error("周期终点不应早于起点（计算溢出？）")
	}
	span := st.WindowEnd.Sub(st.WindowStart)
	if span < 29*24*time.Hour {
		t.Errorf("周期跨度 = %v, 期望约 30 天", span)
	}
}

// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 并发求值
// ---------------------------------------------------------------------------

// TestConcurrentEvaluate 验证并发调用 Evaluate 不会数据竞争或 panic。
//
// 现实中 Evaluate 由定时器驱动，但 WebUI 的手动操作
// （触发/解除）可能与之并发 —— 这正是曾经导致
// "concurrent map writes" 致命崩溃的场景。
func TestConcurrentEvaluate(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	eng.Load([]config.PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}})

	ctx := context.Background()
	var wg sync.WaitGroup
	stop := make(chan struct{})

	// 采样 + 求值
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			fc.addBytes(100)
			mon.PollOnce(ctx, time.Now())
			eng.Evaluate(ctx)
		}
	}()

	// 并发查询
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				_ = eng.States()
				_ = eng.Events(10)
			}
		}()
	}

	// 并发手动操作
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			_ = eng.ForceTrigger(ctx, "p")
			_ = eng.ForceResume(ctx, "p")
		}
	}()

	time.Sleep(200 * time.Millisecond)
	close(stop)
	wg.Wait()
}

// TestConcurrentLoad 验证并发 Load（热重载）与求值同时进行时安全。
func TestConcurrentLoad(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	pc := config.PolicyConfig{
		Name: "p", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}
	eng.Load([]config.PolicyConfig{pc})

	ctx := context.Background()
	var wg sync.WaitGroup
	stop := make(chan struct{})

	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			mon.PollOnce(ctx, time.Now())
			eng.Evaluate(ctx)
		}
	}()

	// 反复热重载
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			eng.Load([]config.PolicyConfig{pc})
		}
	}()

	time.Sleep(150 * time.Millisecond)
	close(stop)
	wg.Wait()
}

// ---------------------------------------------------------------------------
// 轮转边界
// ---------------------------------------------------------------------------

// TestWindowRollAtExactBoundary 验证恰好落在周期边界上的行为。
//
// 边界处理不当会导致「提前重置」或「永不重置」。
func TestWindowRollAtExactBoundary(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	eng.Load([]config.PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}})

	mon.PollOnce(context.Background(), time.Now())
	fc.addBytes(2000)
	mon.PollOnce(context.Background(), time.Now())
	eng.Evaluate(context.Background())

	if !eng.States()[0].Limited {
		t.Fatal("前置条件：应已限速")
	}

	// 把窗口终点精确设为当前时刻
	eng.mu.Lock()
	now := time.Now()
	eng.policies[0].windowEnd = now
	eng.mu.Unlock()

	mon.PollOnce(context.Background(), now)
	eng.Evaluate(context.Background())

	// 正好到期应触发轮转，进而解除限速
	if eng.States()[0].Limited {
		t.Error("到达周期终点时应轮转并解除限速")
	}
	// 轮转后窗口终点必须晚于当前时刻（否则会每 tick 重复轮转）
	eng.mu.Lock()
	end := eng.policies[0].windowEnd
	eng.mu.Unlock()
	if !end.After(time.Now()) {
		t.Errorf("轮转后的终点 %v 应晚于当前时刻", end)
	}
}

// TestManyRapidRolls 验证连续跨越多个周期时不会出错。
//
// 场景：容器被暂停后恢复（如 NAS 休眠），
// 期间跨越了多个周期，恢复后应一次性推进到当前周期。
func TestManyRapidRolls(t *testing.T) {
	fc := newFakeClient("c")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))
	mon := traffic.NewMonitor(reg, time.Second, 100)
	eng := NewEngine(reg, mon, &recordingNotifier{}, func(string, string, ...any) {}, time.UTC)

	yes := true
	eng.Load([]config.PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"*"},
		Window: config.Duration(time.Hour), Align: "rolling",
		ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
		ResumeOnWindowReset: &yes,
	}})

	mon.PollOnce(context.Background(), time.Now())
	fc.addBytes(2000)
	mon.PollOnce(context.Background(), time.Now())
	eng.Evaluate(context.Background())

	// 把窗口终点设为很久以前（相当于休眠了 10 个周期）
	eng.mu.Lock()
	eng.policies[0].windowEnd = time.Now().Add(-10 * time.Hour)
	eng.mu.Unlock()

	eng.Evaluate(context.Background())

	// 应一次推进到当前周期，而不是逐个补
	eng.mu.Lock()
	end := eng.policies[0].windowEnd
	eng.mu.Unlock()
	if !end.After(time.Now()) {
		t.Errorf("应一次推进到未来，实际终点 %v", end)
	}
	// 不应因为「跨越了 10 个周期」而产生 10 次事件
	if n := len(eng.Events(100)); n > 3 {
		t.Errorf("一次求值不应产生过多事件，实际 %d 条", n)
	}
}
