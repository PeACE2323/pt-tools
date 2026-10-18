// Package traffic 负责把客户端采样转换成「周期内的累计上传流量」，
// 并驱动限速状态机。
//
// 设计要点：
//
//  1. 累计量优先使用客户端自报的 uploadedBytes 差值，
//     而不是「速率 × 采样间隔」的累加值——后者在客户端重启或采样丢失时会漂移。
//  2. 客户端重启会导致 uploadedBytes 归零，此时自动退化为速率积分，
//     避免出现巨大的负差值把累计量冲掉。
//  3. 每个 (客户端, 地址族) 维护独立的基线，周期滚动时重置基线。
package traffic

import (
	"context"
	"fmt"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
)

// ---------------------------------------------------------------------------
// 计数器
// ---------------------------------------------------------------------------

// counter 跟踪单个客户端的周期累计上传量。
type counter struct {
	// lastTotal 是上一次看到的客户端自报累计值。
	lastTotal int64
	// lastSpeed 是上一次采样到的速率，用于积分兜底。
	lastSpeed int64
	// lastSeen 是上一次采样时刻。
	lastSeen time.Time
	// hasBaseline 表示是否已经建立基线（首次采样只建立基线，不计入周期）。
	hasBaseline bool
	// windowBytes 是本周期内累计的字节数。
	windowBytes int64
	// totalBytes 是进程启动以来的累计字节数（用于展示）。
	totalBytes int64
	// resets 记录检测到客户端重启的次数。
	resets int
}

// reset 开始一个新周期，保留基线以免丢掉跨周期的差值。
func (c *counter) reset() {
	c.windowBytes = 0
}

// ---------------------------------------------------------------------------
// 采样快照
// ---------------------------------------------------------------------------

// Sample 是一条用于绘制曲线的采样点。
type Sample struct {
	Timestamp time.Time `json:"t"`
	// UpSpeed 是上传速度（字节/秒）。
	UpSpeed int64 `json:"up"`
	// DlSpeed 是下载速度（字节/秒）。
	DlSpeed int64 `json:"dl"`
	// WindowBytes 是该时刻所在周期的累计上传量。
	WindowBytes int64 `json:"win"`
	// Limited 表示该时刻是否处于限速状态。
	Limited bool `json:"lim"`
}

// ClientSnapshot 是单个客户端在某一时刻的完整视图。
type ClientSnapshot struct {
	Name           string    `json:"name"`
	Type           string    `json:"type"`
	URL            string    `json:"url"`
	Healthy        bool      `json:"healthy"`
	LastError      string    `json:"last_error,omitempty"`
	LastSeen       time.Time `json:"last_seen"`
	UpSpeed        int64     `json:"up_speed"`
	UpTotal        int64     `json:"up_total"`
	WindowBytes    int64     `json:"window_bytes"`
	TotalBytes     int64     `json:"total_bytes"`
	DlSpeed        int64     `json:"dl_speed"`
	PeerCount      int       `json:"peers"`
	TorrentCount   int       `json:"torrents"`
	CurrentUpLimit int64     `json:"current_up_limit"`
}

// ---------------------------------------------------------------------------
// 监控器
// ---------------------------------------------------------------------------

// Monitor 周期性采集所有客户端并维护累计量。
type Monitor struct {
	registry *client.Registry
	interval time.Duration

	mu sync.RWMutex
	// counters[clientName]
	counters map[string]*counter
	// snapshots 是各客户端最近一次的完整快照。
	snapshots map[string]ClientSnapshot
	// history 是各客户端的实时曲线（环形缓冲）。
	history     map[string][]Sample
	historyCap  int
	lastPollEnd time.Time
	pollCount   int64
	pollErrors  int64

	// persist 负责把「周期基线」落盘，使容器重启后周期累计能延续。
	// 为 nil 时不做持久化（测试，以及不关心重启续算的场景）。
	persist *persister
	// windowKey 标识当前周期，跨周期后旧基线自动失效。
	windowKey string
	// pendingAccum 暂存从磁盘恢复的「本周期已累计字节数」。
	// 启动时 counters 尚未建立，必须等首轮采样创建计数器后再套用。
	pendingAccum map[string]int64
}

// NewMonitor 创建监控器。
func NewMonitor(reg *client.Registry, interval time.Duration, historyCap int) *Monitor {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	if historyCap <= 0 {
		historyCap = 720
	}
	m := &Monitor{
		registry:   reg,
		interval:   interval,
		counters:   map[string]*counter{},
		snapshots:  map[string]ClientSnapshot{},
		history:    map[string][]Sample{},
		historyCap: historyCap,
	}
	// 为每个已知客户端预建计数器。
	for _, c := range reg.All() {
		m.ensureCounters(c.Name())
	}
	return m
}

// ensureCounters 为指定客户端准备好计数器。调用方需持有写锁。
func (m *Monitor) ensureCounters(name string) *counter {
	c, ok := m.counters[name]
	if !ok {
		c = &counter{}
		m.counters[name] = c
		// 若这个客户端有从磁盘恢复的周期累计，就在这里套用。
		// 必须等到计数器创建之后 —— 启动阶段 counters 还是空的，
		// 那时直接写入会被静默丢弃，导致重启后周期累计从 0 重新开始。
		m.applyPendingBaselineLocked(name, c)
	}
	return c
}

// Interval 返回采样间隔。
func (m *Monitor) Interval() time.Duration { return m.interval }

// PollOnce 对所有客户端采样一次并更新累计量。
// now 是本次采样的时间戳。
func (m *Monitor) PollOnce(ctx context.Context, now time.Time) {
	clients := m.registry.All()

	// 并发采集，避免某个客户端超时拖慢整体节奏。
	type result struct {
		client *client.Managed
		stats  client.Stats
		err    error
	}
	results := make([]result, len(clients))
	var wg sync.WaitGroup
	for i, c := range clients {
		wg.Add(1)
		go func(i int, c *client.Managed) {
			defer wg.Done()
			st, err := c.Fetch(ctx)
			results[i] = result{client: c, stats: st, err: err}
		}(i, c)
	}
	wg.Wait()

	m.mu.Lock()
	defer m.mu.Unlock()

	for _, r := range results {
		name := r.client.Name()
		cnt := m.ensureCounters(name)

		if r.err != nil {
			m.pollErrors++
			// 采集失败：保留上一次快照，仅更新健康状态，不污染累计量。
			snap := m.snapshots[name]
			snap.Name = name
			snap.Type = r.client.Type()
			snap.Healthy = false
			snap.LastError = r.err.Error()
			snap.LastSeen = r.client.LastSeen()
			m.snapshots[name] = snap
			m.appendHistoryLocked(name, Sample{
				Timestamp:   now,
				UpSpeed:     snap.UpSpeed,
				DlSpeed:     snap.DlSpeed,
				WindowBytes: snap.WindowBytes,
			})
			continue
		}

		st := r.stats
		st.Timestamp = now

		if !cnt.hasBaseline {
			// 首次采样：只建立基线，不计入本周期。
			cnt.hasBaseline = true
			cnt.lastTotal = st.UpTotal
			cnt.lastSpeed = st.UpSpeed
			cnt.lastSeen = now
		} else {
			delta := int64(0)
			switch {
			case st.UpTotal > 0:
				if st.UpTotal >= cnt.lastTotal {
					// 正常情况：用客户端自报累计量的差值，精确且不受采样抖动影响。
					delta = st.UpTotal - cnt.lastTotal
				} else {
					// 客户端重启：累计量归零，改用速率积分补上这一段。
					delta = integrate(cnt.lastSpeed, st.UpSpeed, now.Sub(cnt.lastSeen))
					cnt.resets++
				}
				cnt.lastTotal = st.UpTotal
			default:
				// 客户端不提供累计量：只能速率积分。
				delta = integrate(cnt.lastSpeed, st.UpSpeed, now.Sub(cnt.lastSeen))
			}

			// 防御：单次采样增量不可能超过「峰值速率 × 间隔」的合理上限。
			if maxDelta := maxInt64(cnt.lastSpeed, st.UpSpeed) * int64(now.Sub(cnt.lastSeen).Seconds()+1) * 2; maxDelta > 0 && delta > maxDelta {
				delta = maxDelta
			}
			if delta < 0 {
				delta = 0
			}

			cnt.windowBytes += delta
			cnt.totalBytes += delta
			cnt.lastSpeed = st.UpSpeed
			cnt.lastSeen = now
		}

		m.snapshots[name] = ClientSnapshot{
			Name:           name,
			Type:           r.client.Type(),
			URL:            clientURL(r.client),
			Healthy:        true,
			LastSeen:       now,
			UpSpeed:        st.UpSpeed,
			UpTotal:        st.UpTotal,
			WindowBytes:    cnt.windowBytes,
			TotalBytes:     cnt.totalBytes,
			DlSpeed:        st.DlSpeed,
			PeerCount:      st.PeerCount,
			TorrentCount:   st.TorrentCount,
			CurrentUpLimit: st.CurrentUpLimit,
		}

		m.appendHistoryLocked(name, Sample{
			Timestamp:   now,
			UpSpeed:     st.UpSpeed,
			DlSpeed:     st.DlSpeed,
			WindowBytes: cnt.windowBytes,
		})
	}

	m.lastPollEnd = now
	m.pollCount++
}

// appendHistoryLocked 追加一条曲线点，超出容量时丢弃最旧的点。
func (m *Monitor) appendHistoryLocked(name string, s Sample) {
	h := append(m.history[name], s)
	if len(h) > m.historyCap {
		h = h[len(h)-m.historyCap:]
	}
	m.history[name] = h
}

// Aggregate 返回全部客户端汇总后的快照（用于总览页）。
func (m *Monitor) Aggregate() ClientSnapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()

	agg := ClientSnapshot{
		Name:    "__all__",
		Type:    "aggregate",
		Healthy: true,
	}
	for _, snap := range m.snapshots {
		if !snap.Healthy {
			agg.Healthy = false
		}
		agg.UpSpeed += snap.UpSpeed
		agg.UpTotal += snap.UpTotal
		agg.WindowBytes += snap.WindowBytes
		agg.TotalBytes += snap.TotalBytes
		agg.DlSpeed += snap.DlSpeed
		agg.PeerCount += snap.PeerCount
		agg.TorrentCount += snap.TorrentCount
	}
	return agg
}

// Snapshot 返回指定客户端的快照。
func (m *Monitor) Snapshot(name string) (ClientSnapshot, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	s, ok := m.snapshots[name]
	return s, ok
}

// AllSnapshots 返回全部客户端快照，顺序与注册表一致。
func (m *Monitor) AllSnapshots() []ClientSnapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]ClientSnapshot, 0, len(m.snapshots))
	for _, c := range m.registry.All() {
		if s, ok := m.snapshots[c.Name()]; ok {
			out = append(out, s)
		} else {
			out = append(out, ClientSnapshot{
				Name:    c.Name(),
				Type:    c.Type(),
				Healthy: false,
			})
		}
	}
	return out
}

// History 返回指定客户端的曲线数据。为空时返回聚合曲线。
func (m *Monitor) History(name string) []Sample {
	m.mu.RLock()
	defer m.mu.RUnlock()

	if name != "" && name != "__all__" {
		return append([]Sample(nil), m.history[name]...)
	}
	// 聚合：把各客户端的样本按轮次对齐后求和。
	//
	// 时间戳说明：PollOnce 对所有客户端使用同一个 now，
	// 所以同一轮内各客户端的 Timestamp 是【完全相同】的。
	// 因此按时间戳精确分组即可，不需要也不应该设置容差 ——
	// 一旦容差大于采样间隔，相邻两轮就会被误合并，
	// 导致曲线上的累计量被重复累加（实测可达真实值的 2~3 倍），
	// 与顶部卡片显示的数字对不上。
	//
	// 若将来 PollOnce 改为每客户端独立时间戳，应改为按「轮次序号」对齐，
	// 而不是放宽时间容差。
	type acc struct {
		ts  time.Time
		up  int64
		dl  int64
		win int64
	}

	// 用时间戳的纳秒值精确分组。
	merged := map[int64]*acc{}
	var order []int64
	for _, h := range m.history {
		for _, s := range h {
			key := s.Timestamp.UnixNano()
			a, ok := merged[key]
			if !ok {
				a = &acc{ts: s.Timestamp}
				merged[key] = a
				order = append(order, key)
			}
			a.up += s.UpSpeed
			a.dl += s.DlSpeed
			a.win += s.WindowBytes
		}
	}
	sort.Slice(order, func(i, j int) bool { return order[i] < order[j] })

	groups := make([]*acc, 0, len(order))
	for _, k := range order {
		groups = append(groups, merged[k])
	}

	out := make([]Sample, 0, len(groups))
	for _, g := range groups {
		out = append(out, Sample{
			Timestamp:   g.ts,
			UpSpeed:     g.up,
			DlSpeed:     g.dl,
			WindowBytes: g.win,
		})
	}
	return out
}

// AdoptStateFrom 把旧监控器的累计状态迁移过来，
// 用于配置热重载时避免周期累计量被清零。
func (m *Monitor) AdoptStateFrom(old *Monitor) {
	if old == nil || old == m {
		return
	}
	old.mu.RLock()
	oldCounters := old.counters
	oldHistory := old.history
	oldSnapshots := old.snapshots
	oldPollCount := old.pollCount
	oldPollErrors := old.pollErrors
	old.mu.RUnlock()

	m.mu.Lock()
	defer m.mu.Unlock()

	for name, c := range oldCounters {
		dst := m.ensureCounters(name)
		// 复制值而非指针，避免两个监控器共享同一计数器。
		*dst = *c
	}
	// 保留已有快照（视图连续性），但用旧数据补齐本次没有采到的客户端。
	for name, snap := range oldSnapshots {
		if _, exists := m.snapshots[name]; !exists {
			m.snapshots[name] = snap
		}
	}
	for name, h := range oldHistory {
		if len(m.history[name]) == 0 {
			m.history[name] = append([]Sample(nil), h...)
		}
	}
	m.pollCount = oldPollCount
	m.pollErrors = oldPollErrors
}

// ResetWindow 把指定客户端的周期累计清零（进入新周期时调用）。
//
// 注意：必须同时把【快照】里的周期量清零，不能只改计数器。
// 原因：策略引擎判断是否触发限速时读的是 Snapshot().WindowBytes，
// 而快照要等下一次采样（默认 5 秒）才会刷新。
// 若只清零计数器，在这段窗口里引擎仍会读到周期开始前的旧值，
// 可能刚解除限速就立刻又触发，表现为限速反复横跳。
func (m *Monitor) ResetWindow(names []string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	resetOne := func(name string) {
		if c, ok := m.counters[name]; ok {
			c.reset()
		}
		// 同步刷新快照，避免引擎读到过期值
		if snap, ok := m.snapshots[name]; ok {
			snap.WindowBytes = 0
			m.snapshots[name] = snap
		}
		// 同时清掉该客户端的曲线历史。
		//
		// 否则上一个周期的大值会残留在曲线里，
		// 让「周期累计流量」图出现远高于当前值的峰值
		// （用户实测：当前 859 MiB，曲线峰值却有 988 MiB），
		// 看起来像是两个不一致的数字。
		//
		// 累计量曲线表达的是「本周期内的增长」，新周期就该从 0 画起。
		m.history[name] = nil
	}

	if len(names) == 0 {
		for name := range m.counters {
			resetOne(name)
		}
		return
	}
	for _, n := range names {
		resetOne(n)
	}
}

// Stats 返回监控器自身的运行指标。
func (m *Monitor) Stats() map[string]any {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return map[string]any{
		"interval_seconds": m.interval.Seconds(),
		"poll_count":       m.pollCount,
		"poll_errors":      m.pollErrors,
		"last_poll":        m.lastPollEnd,
		"clients":          len(m.snapshots),
	}
}

// Run 启动周期采样循环，直到 ctx 取消。
func (m *Monitor) Run(ctx context.Context) {
	// 立即采样一次以建立基线。
	m.PollOnce(ctx, time.Now())
	m.PersistAfterPoll()

	ticker := time.NewTicker(m.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			m.PollOnce(ctx, now)
			// 在锁外写盘；内部带节流，不会每轮都真的落盘。
			m.PersistAfterPoll()
		}
	}
}

// PersistAfterPoll 在采样结束后记录周期基线。
//
// 在锁外调用，避免持有监控器锁做磁盘 IO。
// 内部由 persister 做 30 秒节流，因此每轮调用开销很小。
func (m *Monitor) PersistAfterPoll() {
	m.mu.RLock()
	p := m.persist
	key := m.windowKey
	m.mu.RUnlock()
	if p == nil {
		return
	}
	m.snapshotForPersist(key)
}

// ---------------------------------------------------------------------------
// 助手
// ---------------------------------------------------------------------------

// Diagnostics 返回用于排查「周期累计为何是某个值」的内部状态。
//
// 动机：用户反馈「周期累计一直显示历史总量」时，
// 只看界面无法区分是「周期没滚动」还是「计数器没清零」，
// 这两者的修复方向完全不同。这里把计数器、快照与基线一并暴露出来，
// 打开 /api/diagnostics 就能直接定位。
func (m *Monitor) Diagnostics() map[string]any {
	m.mu.RLock()
	defer m.mu.RUnlock()

	out := map[string]any{
		"poll_count":  m.pollCount,
		"poll_errors": m.pollErrors,
		"last_poll":   m.lastPollEnd,
		"clients":     map[string]any{},
	}

	per := map[string]any{}
	for name, c := range m.counters {
		entry := map[string]any{
			"window_bytes": c.windowBytes,
			"total_bytes":  c.totalBytes,
			"last_total":   c.lastTotal,
			"last_speed":   c.lastSpeed,
			"has_baseline": c.hasBaseline,
			"resets":       c.resets,
		}
		// 快照里的值（界面直接读这个），应与上面一致
		if snap, ok := m.snapshots[name]; ok {
			entry["snapshot_window"] = snap.WindowBytes
			entry["snapshot_up_speed"] = snap.UpSpeed
		}
		per[name] = entry
	}
	out["clients"] = per
	return out
}

// integrate 用梯形法估算两次采样之间产生的字节数。
func integrate(prevSpeed, curSpeed int64, dt time.Duration) int64 {
	if dt <= 0 {
		return 0
	}
	secs := dt.Seconds()
	// 梯形法：平均速率 × 时间
	avg := (float64(prevSpeed) + float64(curSpeed)) / 2
	return int64(math.Round(avg * secs))
}

// maxInt64 返回较大者（本包内部使用，避免与 client 包的实现冲突）。
func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

// clientURL 返回客户端的地址（从 Managed 中取不到时返回空串）。
func clientURL(c *client.Managed) string {
	type urlProvider interface{ URL() string }
	if u, ok := c.Client.(urlProvider); ok {
		return u.URL()
	}
	return ""
}

// FormatBytes 把字节数格式化为人类可读字符串。
func FormatBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	units := []string{"KiB", "MiB", "GiB", "TiB", "PiB"}
	val := float64(n)
	i := -1
	for val >= unit && i < len(units)-1 {
		val /= unit
		i++
	}
	return fmt.Sprintf("%.2f %s", val, units[i])
}

// FormatSpeed 把字节/秒格式化为人类可读字符串。
func FormatSpeed(n int64) string {
	if n <= 0 {
		return "不限速"
	}
	return FormatBytes(n) + "/s"
}
