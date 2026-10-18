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
// 测试替身
// ---------------------------------------------------------------------------

// fakeClient 是一个可编程的假下载器：能上报给定的速率/累计量，
// 并记录收到的每一次限速指令。
type fakeClient struct {
	name string

	mu         sync.Mutex
	upTotal    int64
	upLimit    int64 // 当前生效的上传限速，0 表示不限速
	setCalls   []int64
	fetchCalls int
}

func newFakeClient(name string) *fakeClient {
	return &fakeClient{name: name}
}

func (f *fakeClient) Name() string { return f.name }
func (f *fakeClient) Type() string { return "fake" }

func (f *fakeClient) Fetch(_ context.Context) (client.Stats, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fetchCalls++
	return client.Stats{
		Timestamp: time.Now(),
		UpSpeed:   1024,
		UpTotal:   f.upTotal,
		DlSpeed:   0,
		// 上报当前生效的限速，供状态机做「限速是否被外部改动」的校验
		CurrentUpLimit: f.upLimit,
	}, nil
}

func (f *fakeClient) SetUploadLimit(_ context.Context, l client.SpeedLimit) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.upLimit = l.Total
	f.setCalls = append(f.setCalls, l.Total)
	return nil
}

// addBytes 模拟客户端又上传了这么多字节。
func (f *fakeClient) addBytes(n int64) {
	f.mu.Lock()
	f.upTotal += n
	f.mu.Unlock()
}

func (f *fakeClient) limit() int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.upLimit
}

func (f *fakeClient) calls() []int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]int64(nil), f.setCalls...)
}

// recordingNotifier 记录收到的通知。
type recordingNotifier struct {
	mu     sync.Mutex
	events []Event
}

func (r *recordingNotifier) Notify(_ context.Context, ev Event) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, ev)
	return nil
}

func (r *recordingNotifier) all() []Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Event(nil), r.events...)
}

// ---------------------------------------------------------------------------
// 测试脚手架
// ---------------------------------------------------------------------------

// harness 把监控器与策略引擎组装成一个可手动推进的测试环境。
type harness struct {
	cli    *fakeClient
	reg    *client.Registry
	mon    *traffic.Monitor
	engine *Engine
	notif  *recordingNotifier
}

func newHarness(t *testing.T, pc config.PolicyConfig) *harness {
	t.Helper()

	fc := newFakeClient("test")
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(fc))

	mon := traffic.NewMonitor(reg, time.Second, 100)
	notif := &recordingNotifier{}

	// 用固定时区，避免测试结果随时区漂移
	loc := time.FixedZone("CST", 8*3600)
	eng := NewEngine(reg, mon, notif, func(string, string, ...any) {}, loc)
	eng.Load([]config.PolicyConfig{pc})

	return &harness{cli: fc, reg: reg, mon: mon, engine: eng, notif: notif}
}

// poll 触发一次采样（让监控器累计流量）。
func (h *harness) poll() {
	h.mon.PollOnce(context.Background(), time.Now())
}

// evaluate 触发一次策略求值。
func (h *harness) evaluate() {
	h.engine.Evaluate(context.Background())
}

// state 返回第一条策略的状态。
func (h *harness) state(t *testing.T) PolicyState {
	t.Helper()
	states := h.engine.States()
	if len(states) == 0 {
		t.Fatal("没有策略状态")
	}
	return states[0]
}

// basePolicy 返回一份用于测试的策略配置。
func basePolicy() config.PolicyConfig {
	yes := true
	return config.PolicyConfig{
		Name:                "test-policy",
		Enabled:             &yes,
		Clients:             []string{"*"},
		Window:              config.Duration(time.Hour),
		Align:               "rolling",
		ThresholdTotal:      config.Bytes(1000),
		LimitTotal:          config.Bitrate(100),
		ResumeOnWindowReset: &yes,
		Cooldown:            config.Duration(0),
	}
}

// ---------------------------------------------------------------------------
// 测试用例
// ---------------------------------------------------------------------------

// TestTriggerAndResume 覆盖核心闭环：达到阈值 → 限速 → 周期滚动 → 恢复不限速。
//
// 这里重点回归一个真实 bug：恢复值必须是客户端**原始**限速（本例为 0=不限速），
// 而不能被我们自己下发的限速值污染，否则一旦触发就再也无法真正恢复。
func TestTriggerAndResume(t *testing.T) {
	h := newHarness(t, basePolicy())

	// 第一次采样只建立基线，不计入周期
	h.poll()
	h.evaluate()
	if got := h.cli.limit(); got != 0 {
		t.Fatalf("基线阶段不应限速，当前限速 = %d", got)
	}

	// 累计量超过阈值 1000
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 100 {
		t.Fatalf("达到阈值后应限速到 100，实际 = %d", got)
	}
	st := h.state(t)
	if !st.Limited {
		t.Error("策略状态应为 limited")
	}

	// 强制把周期推到过去，模拟周期滚动
	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()

	h.poll()
	h.evaluate()

	// 观察恢复事件：这是判断「是否真的恢复过」最可靠的依据。
	//
	// 注意：一次 Evaluate 内可能先恢复、随即又因新周期内累计量再次超标而重新限速，
	// 因此不能只看最终的限速值，必须检查事件序列与下发序列。
	var resumeEvents []Event
	for _, ev := range h.notif.all() {
		if ev.Type == EventResume {
			resumeEvents = append(resumeEvents, ev)
		}
	}
	if len(resumeEvents) == 0 {
		t.Fatalf("周期滚动后未产生恢复事件，限速调用序列 = %v", h.cli.calls())
	}
	// 恢复事件的目标值必须是原始值 0，而不是我们自己下发的 100。
	for _, ev := range resumeEvents {
		if ev.NewLimit != 0 {
			t.Errorf("恢复事件的目标值应为 0，实际 = %d（基线被自己下发的限速污染）", ev.NewLimit)
		}
	}

	// 校验下发的调用序列：首次必须触发限速(100)，且第 2 次必须是恢复(0)。
	calls := h.cli.calls()
	if len(calls) < 2 {
		t.Fatalf("限速调用次数不足: %v", calls)
	}
	if calls[0] != 100 {
		t.Errorf("首次下发应为 100，实际 = %d", calls[0])
	}
	if calls[1] != 0 {
		t.Errorf("恢复下发应为 0（原始值），实际 = %d —— 基线被自己下发的限速污染", calls[1])
	}
	// 后续若再次触发，也只能是配置值 100 或恢复值 0，不能是别的值。
	for i, v := range calls[2:] {
		if v != 100 && v != 0 {
			t.Errorf("第 %d 次限速值异常: %d", i+3, v)
		}
	}

	// 校验通知：至少各有一次触发与恢复。
	var speedups, resumes int
	for _, ev := range h.notif.all() {
		switch ev.Type {
		case EventSpeedUp:
			speedups++
		case EventResume:
			resumes++
		}
	}
	if speedups < 1 || resumes < 1 {
		t.Errorf("通知统计 = speedup:%d resume:%d, 期望至少各 1 次", speedups, resumes)
	}

	// 关键回归点：所有恢复事件的目标值都必须是 0（原始不限速），
	// 绝不能是 100（我们自己下发的值）。
	for _, ev := range h.notif.all() {
		if ev.Type == EventResume && ev.NewLimit != 0 {
			t.Errorf("恢复事件的目标值应为 0，实际 = %d（基线被污染）", ev.NewLimit)
		}
	}
}

// TestNoTriggerBelowThreshold 验证未达阈值时不应限速。
func TestNoTriggerBelowThreshold(t *testing.T) {
	h := newHarness(t, basePolicy())

	h.poll()
	h.cli.addBytes(500) // 低于阈值 1000
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 0 {
		t.Errorf("未达阈值不应限速，实际 = %d", got)
	}
	if st := h.state(t); st.Limited {
		t.Error("策略状态不应为 limited")
	}
}

// TestCooldownDelaysResume 验证冷却期内即使周期滚动也保持限速。
func TestCooldownDelaysResume(t *testing.T) {
	pc := basePolicy()
	pc.Cooldown = config.Duration(time.Hour) // 很长的冷却期
	h := newHarness(t, pc)

	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 100 {
		t.Fatalf("应已限速，实际 = %d", got)
	}

	// 周期滚动，但冷却期未过
	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()

	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 100 {
		t.Errorf("冷却期内应保持限速 100，实际 = %d", got)
	}
}

// TestResumeOnWindowResetFalse 验证关闭自动恢复后，周期滚动仍保持限速。
func TestResumeOnWindowResetFalse(t *testing.T) {
	no := false
	pc := basePolicy()
	pc.ResumeOnWindowReset = &no
	h := newHarness(t, pc)

	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 100 {
		t.Fatalf("应已限速，实际 = %d", got)
	}

	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
	h.engine.mu.Unlock()
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 100 {
		t.Errorf("resume_on_window_reset=false 时应保持限速，实际 = %d", got)
	}
}

// TestWindowRollOnlyOnce 验证周期滚动不会在每个 tick 重复触发。
//
// 回归一个真实 bug：早期实现用 windowStart = windowEnd 推进周期，
// 当新边界仍落在过去时会每个 tick 都清零累计量，导致策略无法稳定触发。
func TestWindowRollOnlyOnce(t *testing.T) {
	h := newHarness(t, basePolicy())

	h.poll()
	h.evaluate()

	// 把窗口终点推到很久以前，模拟进程长时间未求值
	h.engine.mu.Lock()
	h.engine.policies[0].windowEnd = time.Now().Add(-10 * time.Hour)
	h.engine.mu.Unlock()

	h.evaluate()

	// 滚动后，终点必须严格晚于当前时刻
	h.engine.mu.Lock()
	end := h.engine.policies[0].windowEnd
	h.engine.mu.Unlock()

	if !end.After(time.Now()) {
		t.Errorf("滚动后窗口终点 %v 应晚于当前时刻", end)
	}

	// 再求值多次，窗口终点不应被反复推进
	h.evaluate()
	h.evaluate()
	h.engine.mu.Lock()
	end2 := h.engine.policies[0].windowEnd
	h.engine.mu.Unlock()

	if !end2.Equal(end) {
		t.Errorf("窗口终点被重复推进: %v -> %v", end, end2)
	}
}

// TestForceResumeAndTrigger 验证手动解除/触发接口。
func TestForceResumeAndTrigger(t *testing.T) {
	h := newHarness(t, basePolicy())
	h.poll()
	h.evaluate()

	ctx := context.Background()

	if err := h.engine.ForceResume(ctx, "test-policy"); err == nil {
		t.Error("未限速时调用 ForceResume 应当报错")
	}

	if err := h.engine.ForceTrigger(ctx, "test-policy"); err != nil {
		t.Fatalf("ForceTrigger 失败: %v", err)
	}
	if got := h.cli.limit(); got != 100 {
		t.Errorf("手动触发后应限速 100，实际 = %d", got)
	}

	if err := h.engine.ForceTrigger(ctx, "test-policy"); err == nil {
		t.Error("已限速时再次 ForceTrigger 应当报错")
	}

	if err := h.engine.ForceResume(ctx, "test-policy"); err != nil {
		t.Fatalf("ForceResume 失败: %v", err)
	}
	if got := h.cli.limit(); got != 0 {
		t.Errorf("手动解除后应恢复为 0，实际 = %d", got)
	}

	if err := h.engine.ForceResume(ctx, "不存在的策略"); err == nil {
		t.Error("不存在的策略应当报错")
	}
}

// TestAdoptStateFromPreservesWindow 验证配置热重载不会丢失周期进度与限速状态。
func TestAdoptStateFromPreservesWindow(t *testing.T) {
	h := newHarness(t, basePolicy())
	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 100 {
		t.Fatalf("应已限速，实际 = %d", got)
	}

	h.engine.mu.Lock()
	winEndBefore := h.engine.policies[0].windowEnd
	h.engine.mu.Unlock()

	// 模拟热重载：新建监控器与引擎，并按生产代码的顺序继承旧状态。
	//
	// 生产代码（applyConfig）会同时继承监控器与引擎的状态，
	// 这里保持一致，否则快照缓存为空会导致行为与线上不符。
	fc2 := h.cli // 复用同一个假客户端，保持「客户端侧限速值」连续
	reg2 := client.NewRegistry()
	reg2.Add(client.NewManaged(fc2))
	mon2 := traffic.NewMonitor(reg2, time.Second, 100)
	mon2.AdoptStateFrom(h.mon)

	loc := time.FixedZone("CST", 8*3600)
	eng2 := NewEngine(reg2, mon2, h.notif, func(string, string, ...any) {}, loc)
	eng2.AdoptStateFrom(h.engine)
	eng2.Load([]config.PolicyConfig{basePolicy()})

	eng2.mu.Lock()
	winEndAfter := eng2.policies[0].windowEnd
	limited := eng2.policies[0].limited
	eng2.mu.Unlock()

	if !winEndAfter.Equal(winEndBefore) {
		t.Errorf("热重载后窗口终点被改变: %v -> %v", winEndBefore, winEndAfter)
	}
	if !limited {
		t.Error("热重载后限速状态丢失")
	}

	// 继承后先采样再求值，不应重复下发限速。
	before := len(h.cli.calls())
	mon2.PollOnce(context.Background(), time.Now())
	eng2.Evaluate(context.Background())
	after := len(h.cli.calls())
	if after != before {
		t.Errorf("热重载后重复下发了限速: %v", h.cli.calls())
	}
}

// TestBaselinePreservedAcrossCycles 验证多轮「限速→恢复」后仍能恢复到原始限速。
// 这是 baselineLimits 必须「只写一次」的关键保证。
func TestBaselinePreservedAcrossCycles(t *testing.T) {
	h := newHarness(t, basePolicy())

	for cycle := 0; cycle < 3; cycle++ {
		h.poll()
		h.cli.addBytes(2000)
		h.poll()
		h.evaluate()

		if got := h.cli.limit(); got != 100 {
			t.Fatalf("第 %d 轮：应限速 100，实际 = %d", cycle+1, got)
		}

		h.engine.mu.Lock()
		h.engine.policies[0].windowEnd = time.Now().Add(-time.Second)
		h.engine.mu.Unlock()
		h.poll()
		h.evaluate()

		// 恢复动作必须把限速设回原始值 0。
		// （若同一周期内流量再次超标，随后可能再次触发到 100，这是正常行为。）
		calls := h.cli.calls()
		sawZero := false
		for _, v := range calls {
			if v == 0 {
				sawZero = true
			}
		}
		if !sawZero {
			t.Fatalf("第 %d 轮：未观察到恢复为 0 的调用，序列 = %v", cycle+1, calls)
		}
		// 绝不能出现 100 之外的限速值（例如被污染的基线值）
		for _, v := range calls {
			if v != 0 && v != 100 {
				t.Fatalf("第 %d 轮：出现异常限速值 %d，序列 = %v", cycle+1, v, calls)
			}
		}
	}

	// 每一轮至少有一次恢复(0)，共至少 3 次
	var zeros int
	for _, v := range h.cli.calls() {
		if v == 0 {
			zeros++
		}
	}
	if zeros < 3 {
		t.Errorf("恢复为 0 的次数 = %d, 期望至少 3 次，序列 = %v", zeros, h.cli.calls())
	}
}
