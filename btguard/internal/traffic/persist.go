package traffic

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// persistState 是落盘的最小状态。
//
// 目的：容器重启（例如升级镜像）后，周期累计不应清零。
//
// 原理：客户端自己维护的累计量（qBittorrent 的 up_info_data、
// Transmission 的 cumulative-stats.uploadedBytes）是单调递增的，
// 只要记住「周期开始时它等于多少」，重启后就能算出
// 本周期已用了多少：当前值 - 周期基线。
type persistState struct {
	// WindowKey 标识这份基线属于哪个周期，跨周期后自动失效。
	WindowKey string `json:"window_key"`
	// SavedAt 仅用于排查。
	SavedAt time.Time `json:"saved_at"`
	// Accumulated 记录每个客户端在当前周期内【已经累计上传】的字节数。
	//
	// 为什么不直接存「客户端累计量的快照」：
	// 客户端自身可能重启，其累计量会归零；
	// 而「本周期已用多少」是本工具自己的语义，
	// 直接存它，重启后原样恢复即可，不依赖客户端状态是否连续。
	Accumulated map[string]int64 `json:"accumulated"`
}

// persister 负责状态的读写，带节流避免每轮都写盘。
type persister struct {
	path string
	mu   sync.Mutex
	last time.Time
	// every 控制最小写盘间隔；采样间隔通常 5 秒，没必要每次都写。
	every time.Duration
}

func newPersister(path string) *persister {
	// 首次写盘不做限流（last 为零值），随后每 minEvery 秒最多写一次。
	//
	// 节流间隔的选择：写盘要小到「即使容器密集重启也不丢多少流量」，
	// 又要大到「不至于每轮采样都产生磁盘 IO」。
	// 采样间隔通常 5 秒，这里取 10 秒 —— 最坏情况只丢 10 秒的流量，
	// 对按天/按月统计的量级来说可以忽略。
	return &persister{path: path, every: 10 * time.Second}
}

// load 读取状态；文件不存在或损坏时返回 nil（不视为错误）。
func (p *persister) load() *persistState {
	if p == nil || p.path == "" {
		return nil
	}
	raw, err := os.ReadFile(p.path)
	if err != nil {
		return nil
	}
	var st persistState
	if err := json.Unmarshal(raw, &st); err != nil {
		// 文件损坏不应阻止启动，忽略并重建即可
		return nil
	}
	return &st
}

// save 原子写入状态。写入失败只影响「重启后能否续算」，不影响运行，故不报错。
func (p *persister) save(st *persistState) {
	if p == nil || p.path == "" || st == nil {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()

	if time.Since(p.last) < p.every {
		return
	}
	st.SavedAt = time.Now()

	raw, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(p.path), 0o755); err != nil {
		return
	}
	// 先写临时文件再重命名，避免进程在写入途中被杀而留下半截文件。
	tmp := p.path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return
	}
	if err := os.Rename(tmp, p.path); err != nil {
		_ = os.Remove(tmp)
		return
	}
	p.last = time.Now()
}

// forceSave 忽略节流直接写盘（用于周期滚动等关键时点）。
func (p *persister) forceSave(st *persistState) {
	if p == nil {
		return
	}
	p.mu.Lock()
	p.last = time.Time{}
	p.mu.Unlock()
	p.save(st)
}

// SetStatePath 设置状态文件路径，并尝试恢复上一次的周期累计。
//
// windowKey 标识当前周期（例如 "day:2026-10-17"）。
// 只有 key 一致时才恢复 —— 已经跨周期的话，
// 旧数据代表的是上个周期的用量，不能再用。
//
// 恢复的是「本周期已累计的字节数」，直接写进 windowBytes/totalBytes，
// 而不是去动 lastTotal（那是算差值用的，客户端重启会让它失效）。
func (m *Monitor) SetStatePath(path string, windowKey string) {
	p := newPersister(path)

	m.mu.Lock()
	m.persist = p
	m.windowKey = windowKey
	m.mu.Unlock()

	st := p.load()
	if st == nil || st.WindowKey != windowKey || len(st.Accumulated) == 0 {
		return
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	// NewMonitor 已为每个客户端预建计数器，正常情况下可直接套用；
	// 未建好的暂存，等 ensureCounters 创建时补上。
	for name, used := range st.Accumulated {
		c, ok := m.counters[name]
		if !ok {
			if m.pendingAccum == nil {
				m.pendingAccum = map[string]int64{}
			}
			m.pendingAccum[name] = used
			continue
		}
		c.windowBytes = used
		c.totalBytes = used
	}
}

// applyPendingBaselineLocked 在计数器就绪后套用暂存的累计量。
// 调用方必须持有 m.mu。返回是否应用了数据。
func (m *Monitor) applyPendingBaselineLocked(name string, c *counter) bool {
	if len(m.pendingAccum) == 0 {
		return false
	}
	used, ok := m.pendingAccum[name]
	if !ok {
		return false
	}
	c.windowBytes = used
	c.totalBytes = used
	delete(m.pendingAccum, name)
	return true
}

// SetWindowKey 更新周期标识；key 变化时丢弃旧基线（跨周期了）。
func (m *Monitor) SetWindowKey(windowKey string) {
	m.mu.Lock()
	changed := m.windowKey != windowKey
	m.windowKey = windowKey
	p := m.persist
	m.mu.Unlock()

	if changed && p != nil {
		// 周期已变，旧基线作废，用当前值重建。
		m.snapshotForPersist(windowKey)
	}
}

// snapshotForPersist 收集当前各客户端的周期基线并写盘。
//
// 时机：每轮采样后（带节流）、周期滚动时（强制）。
func (m *Monitor) snapshotForPersist(windowKey string) {
	m.mu.Lock()
	p := m.persist
	if p == nil {
		m.mu.Unlock()
		return
	}
	baseline := make(map[string]int64, len(m.counters))
	for name, c := range m.counters {
		// 只记录本周期确实产生过流量的客户端。
		if c.windowBytes > 0 {
			baseline[name] = c.windowBytes
		}
	}
	m.mu.Unlock()

	p.save(&persistState{WindowKey: windowKey, Accumulated: baseline})
}

// ForcePersist 立即写盘（周期滚动后调用，忽略节流）。
func (m *Monitor) ForcePersist(windowKey string) {
	m.mu.Lock()
	p := m.persist
	baseline := make(map[string]int64, len(m.counters))
	for name, c := range m.counters {
		if c.windowBytes > 0 {
			baseline[name] = c.windowBytes
		}
	}
	m.mu.Unlock()

	if p == nil {
		return
	}
	p.forceSave(&persistState{WindowKey: windowKey, Accumulated: baseline})
}
