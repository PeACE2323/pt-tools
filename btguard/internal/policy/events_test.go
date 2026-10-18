package policy

import (
	"context"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
)

// ---------------------------------------------------------------------------
// 事件日志
// ---------------------------------------------------------------------------

// TestEventsReturnsNewestFirst 验证事件按时间倒序返回。
//
// 面板的事件列表依赖这个顺序展示「最近发生了什么」，
// 顺序反了用户会看到最旧的事件排在最前面。
func TestEventsReturnsNewestFirst(t *testing.T) {
	h := newHarness(t, basePolicy())

	// 制造两次状态迁移：触发 -> 解除
	if err := h.engine.ForceTrigger(context.Background(), "test-policy"); err != nil {
		t.Fatalf("触发失败: %v", err)
	}
	if err := h.engine.ForceResume(context.Background(), "test-policy"); err != nil {
		t.Fatalf("解除失败: %v", err)
	}

	events := h.engine.Events(10)
	if len(events) < 2 {
		t.Fatalf("应至少有 2 条事件，实际 %d", len(events))
	}

	// 最新的应是解除（后发生）
	if events[0].Type != EventResume {
		t.Errorf("第一条应是最新的解除事件，实际 %s", events[0].Type)
	}
	// 时间上应严格递减
	for i := 1; i < len(events); i++ {
		if events[i].Time.After(events[i-1].Time) {
			t.Errorf("第 %d 条比前一条更新，顺序应为倒序", i)
		}
	}
}

// TestEventsLimit 验证 limit 参数生效。
func TestEventsLimit(t *testing.T) {
	h := newHarness(t, basePolicy())

	// 制造多条事件
	for i := 0; i < 5; i++ {
		_ = h.engine.ForceTrigger(context.Background(), "test-policy")
		_ = h.engine.ForceResume(context.Background(), "test-policy")
	}

	if got := len(h.engine.Events(3)); got != 3 {
		t.Errorf("limit=3 时返回 %d 条", got)
	}
	// limit 超过总数时返回全部，不应 panic
	all := h.engine.Events(1000)
	if len(all) == 0 {
		t.Error("应返回全部事件")
	}
	// limit <= 0 时返回全部
	if got := len(h.engine.Events(0)); got != len(all) {
		t.Errorf("limit=0 应返回全部（%d），实际 %d", len(all), got)
	}
}

// TestEventsCapturesDetails 验证事件包含排查所需的完整信息。
func TestEventsCapturesDetails(t *testing.T) {
	pc := basePolicy()
	pc.ThresholdTotal = config.Bytes(1000)
	pc.LimitTotal = config.Bitrate(100)
	h := newHarness(t, pc)

	// 累积超过阈值
	h.poll()
	h.cli.addBytes(2000)
	h.poll()
	h.evaluate()

	events := h.engine.Events(10)
	if len(events) == 0 {
		t.Fatal("应产生事件")
	}

	ev := events[0]
	if ev.Type != EventSpeedUp {
		t.Fatalf("首条应为限速触发，实际 %s", ev.Type)
	}
	if ev.Policy != "test-policy" {
		t.Errorf("策略名 = %q", ev.Policy)
	}
	if ev.WindowBytes < 1000 {
		t.Errorf("应记录触发时的累计量，实际 %d", ev.WindowBytes)
	}
	if ev.Threshold != 1000 {
		t.Errorf("应记录阈值，实际 %d", ev.Threshold)
	}
	if ev.NewLimit != 100 {
		t.Errorf("应记录新限速值，实际 %d", ev.NewLimit)
	}
	if ev.Message == "" {
		t.Error("应有人类可读的描述")
	}
	if len(ev.Clients) == 0 {
		t.Error("应记录受影响的客户端")
	}
	if ev.Time.IsZero() {
		t.Error("应记录时间戳")
	}
}

// TestEventsBounded 验证事件数量有上限，不会无限增长。
//
// 长时间运行时若事件无限累积，内存会持续增长。
func TestEventsBounded(t *testing.T) {
	h := newHarness(t, basePolicy())

	// 制造远超上限的事件
	for i := 0; i < 400; i++ {
		_ = h.engine.ForceTrigger(context.Background(), "test-policy")
		_ = h.engine.ForceResume(context.Background(), "test-policy")
	}

	all := h.engine.Events(0)
	if len(all) > h.engine.eventCap {
		t.Errorf("事件数 %d 超过上限 %d", len(all), h.engine.eventCap)
	}
}

// ---------------------------------------------------------------------------
// 阈值判定分支
// ---------------------------------------------------------------------------

// TestStatesReflectProgress 验证状态接口暴露的进度信息正确。
//
// 面板上的进度条直接读这些字段，算错会让用户误判剩余额度。
func TestStatesReflectProgress(t *testing.T) {
	pc := basePolicy()
	pc.ThresholdTotal = config.Bytes(10000)
	h := newHarness(t, pc)

	states := h.engine.States()
	if len(states) != 1 {
		t.Fatalf("应有 1 条策略状态，实际 %d", len(states))
	}
	st := states[0]

	if st.Name != "test-policy" {
		t.Errorf("Name = %q", st.Name)
	}
	if !st.Enabled {
		t.Error("应标记为启用")
	}
	if st.ThresholdTotal != 10000 {
		t.Errorf("ThresholdTotal = %d", st.ThresholdTotal)
	}
	if st.WindowLabel == "" {
		t.Error("应提供可读的周期区间描述")
	}
	if !st.ResumeOnReset {
		t.Error("应反映 resume_on_window_reset 配置")
	}
	if st.Limited {
		t.Error("初始不应处于限速状态")
	}
}

// TestStatesProgressClamped 验证进度值被限制在 [0,1]。
//
// 超出范围会让进度条渲染异常（宽度超过 100%）。
func TestStatesProgressClamped(t *testing.T) {
	pc := basePolicy()
	pc.ThresholdTotal = config.Bytes(100)
	h := newHarness(t, pc)

	// 远超阈值
	h.poll()
	h.cli.addBytes(10000)
	h.poll()

	st := h.engine.States()[0]
	if st.ProgressTotal < 0 || st.ProgressTotal > 1 {
		t.Errorf("ProgressTotal = %v, 应在 [0,1] 内", st.ProgressTotal)
	}
}

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

// TestClamp01 验证归一化函数。
func TestClamp01(t *testing.T) {
	cases := map[float64]float64{
		-1:  0,
		0:   0,
		0.5: 0.5,
		1:   1,
		1.5: 1,
		100: 1,
	}
	for in, want := range cases {
		if got := clamp01(in); got != want {
			t.Errorf("clamp01(%v) = %v, 期望 %v", in, got, want)
		}
	}
}

// TestDescribeLimit 验证限速的人类可读描述。
func TestDescribeLimit(t *testing.T) {
	// 全局限速
	got := describeLimit(client.SpeedLimit{Total: 1 << 20})
	if got == "" {
		t.Error("描述不应为空")
	}

	// 0 表示不限速，应能正确表达
	unlim := describeLimit(client.NewUnlimited())
	if unlim == "" {
		t.Error("不限速的描述不应为空")
	}
}

// TestNearlyEqual 验证近似比较（用于判断限速是否被外部改动）。
func TestNearlyEqual(t *testing.T) {
	// Transmission 的 KB/s 取整会导致几百字节的偏差，需容忍
	if !nearlyEqual(1049000, 1048576, 0.02) {
		t.Error("0.05% 的偏差应在 2% 容差内")
	}
	if !nearlyEqual(100, 100, 0.02) {
		t.Error("完全相等应返回 true")
	}
	if nearlyEqual(100, 200, 0.02) {
		t.Error("相差一倍应返回 false")
	}
	// 期望值为 0 时只有精确相等才算匹配
	if !nearlyEqual(0, 0, 0.02) {
		t.Error("0 与 0 应相等")
	}
	if nearlyEqual(5, 0, 0.02) {
		t.Error("非零与 0 不应相等")
	}
}

// TestNewEngineDefaults 验证引擎在零值参数下也能安全工作。
func TestNewEngineDefaults(t *testing.T) {
	reg := client.NewRegistry()
	mon := traffic.NewMonitor(reg, time.Second, 10)

	// 传入 nil 时区与 nil 日志函数
	eng := NewEngine(reg, mon, nil, nil, nil)
	if eng == nil {
		t.Fatal("应能构造引擎")
	}
	if eng.loc == nil {
		t.Error("时区应回退到默认值而不是 nil")
	}
	if eng.eventCap <= 0 {
		t.Error("事件上限应为正值")
	}
	// 不应 panic
	eng.Evaluate(context.Background())
	_ = eng.States()
	_ = eng.Events(10)
}

// TestLoadIsOrderIndependent 验证 Load 可以被反复调用。
//
// 热重载会多次调用它，且可能在不同时点调用。
func TestLoadIsOrderIndependent(t *testing.T) {
	h := newHarness(t, basePolicy())

	// 反复装载同一配置不应丢失或重复策略
	for i := 0; i < 3; i++ {
		h.engine.Load([]config.PolicyConfig{basePolicy()})
		states := h.engine.States()
		if len(states) != 1 {
			t.Fatalf("第 %d 次装载后策略数 = %d，期望 1", i+1, len(states))
		}
	}

	// 换成不同的策略集合
	pc2 := basePolicy()
	pc2.Name = "another"
	h.engine.Load([]config.PolicyConfig{pc2})
	states := h.engine.States()
	if len(states) != 1 || states[0].Name != "another" {
		t.Errorf("装载新配置后应替换策略，实际 %+v", states)
	}
}

// TestDisabledPolicyDoesNotTrigger 验证禁用的策略不参与求值。
func TestDisabledPolicyDoesNotTrigger(t *testing.T) {
	no := false
	pc := basePolicy()
	pc.Enabled = &no
	h := newHarness(t, pc)

	h.poll()
	h.cli.addBytes(100000) // 远超阈值
	h.poll()
	h.evaluate()

	if got := h.cli.limit(); got != 0 {
		t.Errorf("禁用的策略不应下发限速，实际 %d", got)
	}
	if h.engine.States()[0].Limited {
		t.Error("禁用的策略不应标记为限速中")
	}
}

// TestEvaluateWithNoMatchingClients 验证策略匹配不到客户端时不报错。
func TestEvaluateWithNoMatchingClients(t *testing.T) {
	pc := basePolicy()
	pc.Clients = []string{"nonexistent"}
	// 直接构造，跳过 Validate（它不允许引用不存在的客户端）
	h := newHarness(t, pc)

	h.poll()
	h.poll()
	// 不应 panic
	h.evaluate()
}
