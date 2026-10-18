// Package policy 实现限速状态机。
//
// 每个策略独立维护一条时间线：
//
//	┌──────────┐  周期内累计流量 ≥ 阈值   ┌──────────┐
//	│  normal  │ ───────────────────────▶ │ limited  │
//	└──────────┘                          └──────────┘
//	     ▲                                     │
//	     │        进入新周期且已过冷却期         │
//	     └─────────────────────────────────────┘
//
// 关键设计：
//   - 限速会一直保持，直到「进入新周期」并且「冷却期已过」，避免临界抖动。
//   - 恢复时下发的是配置中的原始限速（通常是 0=不限速），而不是盲目清零。
//   - 每次状态迁移都写审计日志，并在 Web UI 中可见。
package policy

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
)

// ---------------------------------------------------------------------------
// 事件
// ---------------------------------------------------------------------------

// EventType 是策略状态机的迁移事件类型。
type EventType string

const (
	// EventSpeedUp 表示触发了限速。
	EventSpeedUp EventType = "speedup"
	// EventResume 表示解除了限速。
	EventResume EventType = "resume"
	// EventError 表示下发限速失败。
	EventError EventType = "error"
)

// Event 是一次状态迁移记录。
type Event struct {
	Time    time.Time `json:"time"`
	Type    EventType `json:"type"`
	Policy  string    `json:"policy"`
	Clients []string  `json:"clients"`
	Message string    `json:"message"`
	// WindowBytes 是触发时的周期累计量。
	WindowBytes int64 `json:"window_bytes"`
	// Threshold 是触发阈值。
	Threshold int64 `json:"threshold"`
	// OldLimit / NewLimit 是变更前后的上传限速（字节/秒）。
	OldLimit int64 `json:"old_limit"`
	NewLimit int64 `json:"new_limit"`
}

// ---------------------------------------------------------------------------
// 策略运行时状态
// ---------------------------------------------------------------------------

// familyStat 记录某地址族在一个周期内的判断结果。
type familyStat struct {
	triggered bool
	bytes     int64
	threshold int64
}

// runtime 是单条策略的运行状态。
type runtime struct {
	cfg config.PolicyConfig

	// windowStart / windowEnd 是当前周期的边界。
	windowStart time.Time
	windowEnd   time.Time
	// windowAlign / windowLen 记录边界是按什么周期定义算出来的，
	// 用于在配置热重载时判断是否需要重算边界。
	windowAlign string
	windowLen   time.Duration

	// limited 为 true 时表示该策略已触发限速。
	limited bool
	// limitedAt 是触发时刻，用于冷却判断。
	limitedAt time.Time
	// appliedLimits 记录我们对每个客户端下发的限速（用于恢复与展示）。
	appliedLimits map[string]client.SpeedLimit
	// baselineLimits 记录触发前的客户端原始限速，恢复时回填。
	//
	// 该值在客户端首次被本策略限速时建立，之后不再改变，
	// 这样多轮「限速 → 解除」循环后仍然能恢复到真正的原始值。
	baselineLimits map[string]client.SpeedLimit
	// baselineKnown 标记某个客户端是否已经建立过基线。
	baselineKnown map[string]bool
	// exceededBytes 是触发时的周期累计量，用于事件展示。
	exceededBytes int64
	exceededThres int64
	// lastError 记录最近一次下发失败的原因。
	lastError string
}

// ---------------------------------------------------------------------------
// 引擎
// ---------------------------------------------------------------------------

// Engine 是所有策略的调度器。
type Engine struct {
	registry *client.Registry
	monitor  *traffic.Monitor
	notify   Notifier
	logf     func(level, format string, args ...any)

	mu       sync.Mutex
	policies []*runtime
	events   []Event
	eventCap int
	// startedAt 用于 rolling 周期锚点与运行时长展示。
	startedAt time.Time
	loc       *time.Location
	// pending 暂存从旧引擎继承来的状态，等策略装载后合并。
	pending map[string]*runtime
}

// Notifier 是通知发送接口（由 notify 包实现）。
type Notifier interface {
	Notify(ctx context.Context, ev Event) error
}

// NewEngine 构造策略引擎。
func NewEngine(reg *client.Registry, mon *traffic.Monitor, notifier Notifier,
	logf func(level, format string, args ...any), loc *time.Location) *Engine {

	if loc == nil {
		loc = time.Local
	}
	if logf == nil {
		logf = func(string, string, ...any) {}
	}
	return &Engine{
		registry:  reg,
		monitor:   mon,
		notify:    notifier,
		logf:      logf,
		eventCap:  500,
		startedAt: time.Now(),
		loc:       loc,
	}
}

// Load 载入（或重新载入）策略配置。
// 已经处于限速状态的策略如果在新配置中仍然存在，会保留其限速状态。
func (e *Engine) Load(cfgs []config.PolicyConfig) {
	e.mu.Lock()
	defer e.mu.Unlock()

	existing := map[string]*runtime{}
	for _, rt := range e.policies {
		existing[strings.ToLower(rt.cfg.Name)] = rt
	}

	next := make([]*runtime, 0, len(cfgs))
	for _, pc := range cfgs {
		key := strings.ToLower(pc.Name)
		if old, ok := existing[key]; ok {
			// 复用运行状态，但用新配置覆盖策略参数。
			old.cfg = pc
			if old.appliedLimits == nil {
				old.appliedLimits = map[string]client.SpeedLimit{}
			}
			if old.baselineLimits == nil {
				old.baselineLimits = map[string]client.SpeedLimit{}
			}
			if old.baselineKnown == nil {
				old.baselineKnown = map[string]bool{}
			}
			// 周期定义（长度或对齐方式）发生变化时才重算边界；
			// 否则保留现有窗口，避免每次改配置都把累计量清零。
			if old.windowEnd.IsZero() || old.windowAlign != pc.Align || old.windowLen != pc.Window.Std() {
				old.windowStart, old.windowEnd = config.WindowBounds(pc.Align, pc.Window.Std(), time.Now(), e.startedAt, e.loc)
				old.windowAlign = pc.Align
				old.windowLen = pc.Window.Std()
			}
			next = append(next, old)
			continue
		}
		start, end := config.WindowBounds(pc.Align, pc.Window.Std(), time.Now(), e.startedAt, e.loc)
		next = append(next, &runtime{
			cfg:            pc,
			windowStart:    start,
			windowEnd:      end,
			windowAlign:    pc.Align,
			windowLen:      pc.Window.Std(),
			appliedLimits:  map[string]client.SpeedLimit{},
			baselineLimits: map[string]client.SpeedLimit{},
			baselineKnown:  map[string]bool{},
		})
	}
	e.policies = next

	// 若此前通过 AdoptStateFrom 暂存了旧引擎状态，此刻策略已就位，合并之。
	if e.pending != nil {
		pending := e.pending
		e.pending = nil
		e.mergeAdoptedInner(pending, nil, time.Time{})
	}
}

// AdoptStateFrom 把旧引擎的运行状态（窗口进度、限速状态、事件记录）
// 迁移到本引擎，用于配置热重载时避免丢失周期进度。
//
// 调用顺序无关：既可以在 Load 之前调用，也可以在之后调用。
// 状态会先暂存，等策略被 Load 装载后再合并。
// 仅当策略名与周期定义一致时才迁移。
func (e *Engine) AdoptStateFrom(old *Engine) {
	if old == nil || old == e {
		return
	}
	old.mu.Lock()
	prev := make(map[string]*runtime, len(old.policies))
	for _, rt := range old.policies {
		prev[strings.ToLower(rt.cfg.Name)] = rt
	}
	oldEvents := append([]Event(nil), old.events...)
	oldStarted := old.startedAt
	old.mu.Unlock()

	e.mu.Lock()
	// 暂存旧状态，供 mergeAdoptedLocked 使用（可能稍后才 Load）。
	e.pending = prev
	e.mu.Unlock()

	e.mergeAdoptedLocked(prev, oldEvents, oldStarted)
}

// mergeAdoptedLocked 把暂存的旧状态合并进当前已装载的策略。
// 调用方需自行持有锁。
func (e *Engine) mergeAdoptedLocked(prev map[string]*runtime, oldEvents []Event, oldStarted time.Time) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.mergeAdoptedInner(prev, oldEvents, oldStarted)
}

// mergeAdoptedInner 是 mergeAdoptedLocked 的实现，要求调用方已持有锁。
func (e *Engine) mergeAdoptedInner(prev map[string]*runtime, oldEvents []Event, oldStarted time.Time) {
	if prev == nil {
		return
	}

	for _, rt := range e.policies {
		p, ok := prev[strings.ToLower(rt.cfg.Name)]
		if !ok {
			continue
		}
		// 周期定义变了就不继承进度，避免出现错位的窗口。
		if p.windowAlign != rt.windowAlign || p.windowLen != rt.windowLen {
			continue
		}
		rt.windowStart = p.windowStart
		rt.windowEnd = p.windowEnd
		rt.limited = p.limited
		rt.limitedAt = p.limitedAt
		rt.exceededBytes = p.exceededBytes
		rt.exceededThres = p.exceededThres
		rt.lastError = p.lastError
		if p.appliedLimits != nil {
			rt.appliedLimits = p.appliedLimits
		}
		if p.baselineLimits != nil {
			rt.baselineLimits = p.baselineLimits
		}
		if p.baselineKnown != nil {
			rt.baselineKnown = p.baselineKnown
		}
	}
	if len(oldEvents) > 0 {
		e.events = append(oldEvents, e.events...)
		if len(e.events) > e.eventCap {
			e.events = e.events[len(e.events)-e.eventCap:]
		}
	}
	if !oldStarted.IsZero() {
		e.startedAt = oldStarted
	}
}

// Evaluate 对所有策略求值一次。ctx 用于下发限速调用。
//
// 并发安全说明（重要）：
// 该方法会在整个求值过程中持有引擎锁。
// 原因是策略的运行状态（limited / appliedLimits / baselineLimits 等，
// 其中多个是 map）会在求值中被读写，而这些访问此前没有受到保护。
//
// 具体后果：Evaluate 由定时器每秒调用一次，而 WebUI 的
// 「测试限速 / 解除限速」按钮会在 HTTP handler 的独立 goroutine
// 中调用 ForceTrigger / ForceResume。两者同时执行会触发
// Go 运行时的 "concurrent map writes"，这是【不可恢复的致命错误】——
// 进程会直接终止，而不是返回 error，表现为容器无故退出。
//
// 代价：下发限速时会持有锁（网络调用），可能阻塞状态查询。
// 这是有意的取舍 —— 正确性优先于查询延迟，
// 且单次 HTTP 调用通常只有毫秒级。
func (e *Engine) Evaluate(ctx context.Context) {
	e.mu.Lock()
	defer e.mu.Unlock()

	loc := e.loc
	startedAt := e.startedAt
	policies := e.policies

	now := time.Now()

	for _, rt := range policies {
		// 周期滚动必须先于「是否启用」的判断。
		//
		// 原因：总览页的「周期累计」读的是 monitor 的计数器，
		// 而计数器只在 rollWindow 里清零。若这里先 continue，
		// 禁用的策略就永远不会滚动周期，
		// 界面会一直显示历史累计总量（例如昨天的 2.03 TiB），
		// 让人误以为「今天的流量已经这么多了」。
		//
		// 滚动本身只做计数与边界推进，不会下发限速，
		// 因此对禁用的策略执行它是安全的。
		if rt.windowEnd.IsZero() || !now.Before(rt.windowEnd) {
			e.rollWindow(ctx, rt, now, startedAt, loc)
		}

		if !rt.cfg.IsEnabled() {
			continue
		}

		// --- 2. 读取周期累计量 ---
		targets := e.registry.Resolve(rt.cfg.Clients)
		if len(targets) == 0 {
			continue
		}

		totals := e.collectWindowBytes(targets)

		// --- 3. 触发判断 ---
		if !rt.limited {
			reason, bytes, thres := e.shouldTrigger(ctx, rt, totals)
			if reason != "" {
				e.applyLimit(ctx, rt, targets, bytes, thres, reason)
			}
		} else {
			// 已限速：检查是否需要重新下发（例如客户端重启后限速丢失）。
			e.verifyLimit(ctx, rt, targets)
		}
	}
}

// collectWindowBytes 汇总目标客户端在本周期的累计上传量。
func (e *Engine) collectWindowBytes(targets []*client.Managed) int64 {
	var total int64
	for _, c := range targets {
		snap, ok := e.monitor.Snapshot(c.Name())
		if !ok {
			continue
		}
		total += snap.WindowBytes
	}
	return total
}

// shouldTrigger 判断是否应当触发限速，返回触发描述与相关数据。
func (e *Engine) shouldTrigger(_ context.Context, rt *runtime, total int64) (reason string, bytes, thres int64) {
	th := rt.cfg.ThTotal()
	if th <= 0 || total < th {
		return "", 0, 0
	}
	reason = fmt.Sprintf("本周期上传 %s ≥ 阈值 %s",
		traffic.FormatBytes(total), traffic.FormatBytes(th))
	return reason, total, th
}

// applyLimit 下发限速并把策略置为 limited。
func (e *Engine) applyLimit(ctx context.Context, rt *runtime, targets []*client.Managed,
	bytes, thres int64, reason string) {

	limit := client.SpeedLimit{Total: rt.cfg.LimTotal()}
	// LimitTotal 默认为 0（不限速）在触发场景下没有意义，
	// 因此若用户忘了配置，给一个保守的默认值 1 MiB/s。
	if limit.Total <= 0 {
		limit.Total = 1 << 20
	}

	applied := []string{}
	failed := []string{}
	for _, c := range targets {
		// 记录触发前的限速，便于恢复。
		//
		// 这里刻意「只记录一次、永不覆盖」：如果每次触发都重新采样客户端
		// 当前限速，采到的很可能是我们自己上一次下发的限速值
		// （Transmission 还会因 KB/s 取整而与下发值不等），
		// 结果就是恢复时把限速值当成原始值，流量被永久压制。
		//
		// 因此基线只在客户端「从未被本策略改过」时建立一次，
		// 由 baselineKnown 标记保证。
		if !rt.baselineKnown[c.Name()] {
			if rt.baselineLimits == nil {
				rt.baselineLimits = map[string]client.SpeedLimit{}
			}
			if rt.baselineKnown == nil {
				rt.baselineKnown = map[string]bool{}
			}
			base := client.NewUnlimited()
			if snap, ok := e.monitor.Snapshot(c.Name()); ok && snap.CurrentUpLimit > 0 {
				// 客户端确实处于限速状态，且此时我们还没改过它，
				// 说明这是用户自己的设置，恢复时应回到该值。
				base = client.SpeedLimit{Total: snap.CurrentUpLimit}
			}
			rt.baselineLimits[c.Name()] = base
			rt.baselineKnown[c.Name()] = true
			e.logf("debug", "策略 %s: 记录客户端 %s 的原始限速基线为 %s",
				rt.cfg.Name, c.Name(), traffic.FormatSpeed(base.Total))
		}

		if err := c.SetUploadLimit(ctx, limit); err != nil {
			failed = append(failed, fmt.Sprintf("%s: %v", c.Name(), err))
			continue
		}
		rt.appliedLimits[c.Name()] = limit
		applied = append(applied, c.Name())
	}

	rt.limited = true
	rt.limitedAt = time.Now()
	rt.exceededBytes = bytes
	rt.exceededThres = thres

	msg := fmt.Sprintf("策略 %q 触发限速：%s。已对 %d 个客户端设置上传限速为 %s。",
		rt.cfg.Name, reason, len(applied), describeLimit(limit))

	if len(failed) > 0 {
		rt.lastError = strings.Join(failed, "; ")
		e.logf("error", "策略 %s 部分客户端限速失败: %s", rt.cfg.Name, rt.lastError)
		e.recordEvent(Event{
			Time: time.Now(), Type: EventError, Policy: rt.cfg.Name,
			Clients: failed, Message: "下发限速失败: " + rt.lastError,
			WindowBytes: bytes, Threshold: thres,
			OldLimit: e.avgBaseline(rt), NewLimit: limit.Total,
		})
	} else {
		rt.lastError = ""
	}

	e.logf("warn", "%s", msg)
	ev := Event{
		Time: time.Now(), Type: EventSpeedUp, Policy: rt.cfg.Name,
		Clients: applied, Message: msg,
		WindowBytes: bytes, Threshold: thres,
		OldLimit: e.avgBaseline(rt), NewLimit: limit.Total,
	}
	e.recordEvent(ev)

	if e.notify != nil && len(applied) > 0 {
		if err := e.notify.Notify(ctx, ev); err != nil {
			e.logf("warn", "发送限速通知失败: %v", err)
		}
	}
}

// rollWindow 把策略推进到 now 所在的周期，并在跨越边界时重置累计量、
// 按需解除限速。
//
// 实现要点：用循环推进而不是一次赋值，这样即使进程长时间没求值
// （例如被挂起、或窗口很短而求值间隔较长），也只会重置换算一次，
// 且最终边界一定落在 now 之后。
func (e *Engine) rollWindow(ctx context.Context, rt *runtime, now, startedAt time.Time, loc *time.Location) {
	oldStart, oldEnd := rt.windowStart, rt.windowEnd

	// 计算当前时刻所在周期的边界。
	align := rt.cfg.Align
	win := rt.cfg.Window.Std()
	if win <= 0 {
		win = 24 * time.Hour
	}

	var newStart, newEnd time.Time
	if align == "rolling" {
		// 滚动窗口：以 startedAt 为锚点做整数倍推进。
		anchor := startedAt
		if anchor.IsZero() {
			anchor = now
		}
		if oldStart.IsZero() {
			// 首次初始化：从当前时刻开始一个完整周期。
			anchor = now
		}
		elapsed := now.Sub(anchor)
		if elapsed < 0 {
			elapsed = 0
		}
		periods := elapsed / win
		newStart = anchor.Add(periods * win)
		newEnd = newStart.Add(win)
		// 保证边界严格晚于 now（浮点/边界相等场景）。
		for !newEnd.After(now) {
			newStart = newEnd
			newEnd = newStart.Add(win)
		}
	} else {
		newStart, newEnd = config.WindowBounds(align, win, now, startedAt, loc)
		for !newEnd.After(now) {
			// 自然周期一般不会走到这里，防御性推进。
			newStart, newEnd = config.WindowBounds(align, win, newEnd.Add(time.Second), startedAt, loc)
		}
	}

	rt.windowStart = newStart
	rt.windowEnd = newEnd

	// 首次初始化：只设定边界，不当作「跨越周期」处理。
	if oldEnd.IsZero() {
		return
	}

	// 重置该策略所涉客户端的周期累计。
	names := make([]string, 0, 4)
	for _, c := range e.registry.Resolve(rt.cfg.Clients) {
		names = append(names, c.Name())
	}
	e.monitor.ResetWindow(names)

	// 周期已切换，立即把新基线落盘（忽略节流）。
	// 否则容器在「周期开始后、首次定期写盘之前」重启，
	// 会丢掉这段已经产生的流量。
	newKey := align + ":" + newStart.Format("2006-01-02T15:04")
	e.monitor.SetWindowKey(newKey)
	e.monitor.ForcePersist(newKey)

	e.logf("info", "策略 %q 进入新周期（%s → %s），已重置 %d 个客户端的累计流量: %v",
		rt.cfg.Name,
		oldEnd.In(loc).Format("01-02 15:04:05"),
		newEnd.In(loc).Format("01-02 15:04:05"),
		len(names), names)

	if !rt.limited {
		return
	}

	// 冷却期未过则继续限速。
	cooldown := rt.cfg.Cooldown.Std()
	if cooldown > 0 && time.Since(rt.limitedAt) < cooldown {
		e.logf("info", "策略 %q 周期已滚动，但冷却期未结束（剩余 %s），继续保持限速",
			rt.cfg.Name, (cooldown - time.Since(rt.limitedAt)).Round(time.Second))
		return
	}

	if !rt.cfg.ShouldResume() {
		e.logf("info", "策略 %q 配置为不自动解除限速，保持当前限速", rt.cfg.Name)
		return
	}

	e.resumeLimit(ctx, rt, "进入新周期")
}

// resumeLimit 解除限速。
//
// 默认恢复到客户端的【原始限速】（触发前读到的值，通常是「不限速」）。
// 若策略配置了 resume_limit_*，则改为下发该自定义速度 ——
// 典型场景：周期结束后不想立刻全速放开（怕又瞬间冲满额度），
// 而是先回到一个中间速度。
func (e *Engine) resumeLimit(ctx context.Context, rt *runtime, reason string) {
	targets := e.registry.Resolve(rt.cfg.Clients)
	restored := []string{}
	failed := []string{}
	var newLimit int64

	// 配了 resume_limit_* 就使用自定义恢复速度，不再回到原始限速。
	useCustom := rt.cfg.HasResumeLimit()

	for _, c := range targets {
		var restore client.SpeedLimit

		if useCustom {
			restore = client.SpeedLimit{Total: rt.cfg.ResumeTotal()}
		} else {
			base, ok := rt.baselineLimits[c.Name()]
			if !ok {
				base = client.NewUnlimited()
			}
			// 基线里 Total 为 -1（未知）时按不限速处理。
			restore = client.SpeedLimit{Total: base.Total}
			if restore.Total < 0 {
				restore.Total = 0
			}
		}

		if err := c.SetUploadLimit(ctx, restore); err != nil {
			failed = append(failed, fmt.Sprintf("%s: %v", c.Name(), err))
			continue
		}
		restored = append(restored, c.Name())
		newLimit = restore.Total
	}

	rt.limited = false
	rt.appliedLimits = map[string]client.SpeedLimit{}
	// 注意：baselineLimits / baselineKnown 必须保留，
	// 它们是客户端真正的原始限速，跨周期复用，不因恢复而清除。
	rt.exceededBytes = 0
	rt.exceededThres = 0
	rt.lastError = ""

	msg := fmt.Sprintf("策略 %q 已解除限速（%s），%d 个客户端恢复为 %s。",
		rt.cfg.Name, reason, len(restored), traffic.FormatSpeed(newLimit))
	if len(failed) > 0 {
		rt.lastError = strings.Join(failed, "; ")
		e.logf("error", "策略 %s 恢复限速失败: %s", rt.cfg.Name, rt.lastError)
		e.recordEvent(Event{
			Time: time.Now(), Type: EventError, Policy: rt.cfg.Name,
			Clients: failed, Message: "解除限速失败: " + rt.lastError,
		})
	} else {
		e.logf("info", "%s", msg)
	}

	ev := Event{
		Time: time.Now(), Type: EventResume, Policy: rt.cfg.Name,
		Clients: restored, Message: msg, NewLimit: newLimit,
	}
	e.recordEvent(ev)

	if e.notify != nil && len(restored) > 0 {
		if err := e.notify.Notify(ctx, ev); err != nil {
			e.logf("warn", "发送恢复通知失败: %v", err)
		}
	}
}

// verifyLimit 确保限速仍然生效（客户端重启可能丢失限速设置）。
func (e *Engine) verifyLimit(ctx context.Context, rt *runtime, targets []*client.Managed) {
	for _, c := range targets {
		want, ok := rt.appliedLimits[c.Name()]
		if !ok {
			continue
		}
		snap, ok := e.monitor.Snapshot(c.Name())
		if !ok || !snap.Healthy {
			continue
		}
		// CurrentUpLimit == -1 表示客户端未提供该字段，跳过校验。
		if snap.CurrentUpLimit < 0 {
			continue
		}
		// 允许 1% 的误差（Transmission 的 KB/s 取整）。
		if want.Total > 0 && !nearlyEqual(snap.CurrentUpLimit, want.Total, 0.02) {
			e.logf("info", "客户端 %s 限速被外部修改（当前 %s，期望 %s），重新下发",
				c.Name(), traffic.FormatSpeed(snap.CurrentUpLimit), traffic.FormatSpeed(want.Total))
			if err := c.SetUploadLimit(ctx, want); err != nil {
				e.logf("warn", "重新下发 %s 限速失败: %v", c.Name(), err)
			}
		}
	}
}

// ---------------------------------------------------------------------------
// 查询接口（供 Web UI 使用）
// ---------------------------------------------------------------------------

// PolicyState 是策略的对外视图。
type PolicyState struct {
	Name    string `json:"name"`
	Enabled bool   `json:"enabled"`
	Align   string `json:"align"`
	Window  string `json:"window"`

	WindowStart time.Time `json:"window_start"`
	WindowEnd   time.Time `json:"window_end"`
	WindowLabel string    `json:"window_label"`

	ThresholdTotal int64 `json:"threshold_total"`
	LimitTotal     int64 `json:"limit_total"`

	Clients []string `json:"clients"`

	// 运行状态
	Limited       bool      `json:"limited"`
	LimitedAt     time.Time `json:"limited_at"`
	LimitedFor    string    `json:"limited_for"`
	CurrentTotal  int64     `json:"current_total"`
	ProgressTotal float64   `json:"progress_total"`
	LastError     string    `json:"last_error,omitempty"`
	Cooldown      string    `json:"cooldown"`
	ResumeOnReset bool      `json:"resume_on_window_reset"`
}

// States 返回所有策略的当前状态。
//
// 全程持有引擎锁：它会读取各策略的运行状态，
// 而 Evaluate / Force* 会写这些字段。若不加锁读取，
// 除了数据竞争之外，还可能在 map 扩容读到不一致的中间态。
func (e *Engine) States() []PolicyState {
	e.mu.Lock()
	defer e.mu.Unlock()

	loc := e.loc
	policies := e.policies

	now := time.Now()
	out := make([]PolicyState, 0, len(policies))
	for _, rt := range policies {
		targets := e.registry.Resolve(rt.cfg.Clients)
		total := e.collectWindowBytes(targets)

		st := PolicyState{
			Name:           rt.cfg.Name,
			Enabled:        rt.cfg.IsEnabled(),
			Align:          rt.cfg.Align,
			Window:         rt.cfg.Window.String(),
			WindowStart:    rt.windowStart,
			WindowEnd:      rt.windowEnd,
			WindowLabel:    fmt.Sprintf("%s → %s", rt.windowStart.In(loc).Format("01-02 15:04"), rt.windowEnd.In(loc).Format("01-02 15:04")),
			ThresholdTotal: rt.cfg.ThTotal(),
			LimitTotal:     rt.cfg.LimTotal(),
			Clients:        rt.cfg.Clients,
			Limited:        rt.limited,
			LimitedAt:      rt.limitedAt,
			CurrentTotal:   total,
			LastError:      rt.lastError,
			Cooldown:       rt.cfg.Cooldown.String(),
			ResumeOnReset:  rt.cfg.ShouldResume(),
		}
		if rt.limited && !rt.limitedAt.IsZero() {
			st.LimitedFor = time.Since(rt.limitedAt).Round(time.Second).String()
		}
		// 时间维度进度（用于展示周期内剩余时间）。
		if span := rt.windowEnd.Sub(rt.windowStart); span > 0 {
			elapsed := now.Sub(rt.windowStart)
			st.ProgressTotal = clamp01(float64(elapsed) / float64(span))
		}
		out = append(out, st)
	}
	return out
}

// Events 返回最近的事件记录（最新在前）。
func (e *Engine) Events(limit int) []Event {
	e.mu.Lock()
	defer e.mu.Unlock()
	if limit <= 0 || limit > len(e.events) {
		limit = len(e.events)
	}
	out := make([]Event, 0, limit)
	for i := len(e.events) - 1; i >= 0 && len(out) < limit; i-- {
		out = append(out, e.events[i])
	}
	return out
}

// ForceResume 手动解除某个策略的限速。
//
// 与 Evaluate 一样，整个操作过程中持有引擎锁：
// resumeLimit 会读写策略的运行状态（含 map），
// 若与定时求值并发执行会触发 "concurrent map writes" 致命错误。
func (e *Engine) ForceResume(ctx context.Context, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()

	var target *runtime
	for _, rt := range e.policies {
		if strings.EqualFold(rt.cfg.Name, name) {
			target = rt
			break
		}
	}

	if target == nil {
		return fmt.Errorf("未找到策略 %q", name)
	}
	if !target.limited {
		return fmt.Errorf("策略 %q 当前未处于限速状态", name)
	}
	e.resumeLimit(ctx, target, "手动解除")
	return nil
}

// ForceTrigger 手动触发某个策略的限速（用于测试通知与限速是否可用）。
//
// 同样在全程持有引擎锁，理由见 ForceResume 的说明。
func (e *Engine) ForceTrigger(ctx context.Context, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()

	var target *runtime
	for _, rt := range e.policies {
		if strings.EqualFold(rt.cfg.Name, name) {
			target = rt
			break
		}
	}

	if target == nil {
		return fmt.Errorf("未找到策略 %q", name)
	}
	if target.limited {
		return fmt.Errorf("策略 %q 已处于限速状态", name)
	}
	targets := e.registry.Resolve(target.cfg.Clients)
	if len(targets) == 0 {
		return fmt.Errorf("策略 %q 没有匹配到任何客户端", name)
	}
	total := e.collectWindowBytes(targets)
	e.applyLimit(ctx, target, targets, total, target.cfg.ThTotal(), "手动触发")
	return nil
}

// ---------------------------------------------------------------------------
// 内部助手
// ---------------------------------------------------------------------------

// recordEvent 追加事件并裁剪长度。调用方需持有锁。
func (e *Engine) recordEvent(ev Event) {
	e.events = append(e.events, ev)
	if len(e.events) > e.eventCap {
		e.events = e.events[len(e.events)-e.eventCap:]
	}
}

// avgBaseline 返回触发前各客户端限速的平均值（用于事件展示）。
func (e *Engine) avgBaseline(rt *runtime) int64 {
	if len(rt.baselineLimits) == 0 {
		return -1
	}
	var sum int64
	var n int64
	for _, l := range rt.baselineLimits {
		if l.Total >= 0 {
			sum += l.Total
			n++
		}
	}
	if n == 0 {
		return -1
	}
	return sum / n
}

// describeLimit 生成限速的可读描述。
func describeLimit(l client.SpeedLimit) string {
	return traffic.FormatSpeed(l.Total)
}

func nearlyEqual(a, b int64, tol float64) bool {
	if b == 0 {
		return a == 0
	}
	diff := float64(a - b)
	if diff < 0 {
		diff = -diff
	}
	return diff/float64(b) <= tol
}

func clamp01(f float64) float64 {
	if f < 0 {
		return 0
	}
	if f > 1 {
		return 1
	}
	return f
}

func ratio(v, total int64) float64 {
	if total <= 0 {
		return 0
	}
	return clamp01(float64(v) / float64(total))
}
