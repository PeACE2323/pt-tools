// Package client 定义了下载器监控与限速的统一抽象。
//
// 每个实现只需要回答两个问题：
//  1. 当前上传速度与累计上传量是多少（单一总量，不区分地址族）
//  2. 把上传限速调到多少
//
// 关于 IPv4 / IPv6：
// 早期版本尝试区分地址族，但 qBittorrent 与 Transmission 的公开 API
// 都不提供可靠的分族累计量 —— Transmission 只能按当前 peer 占比估算，
// qBittorrent 的 peer 明细在保种空闲时经常缺失。
// 强行估算会产生数量级错误（曾出现 2 TB 级的虚假累计），
// 因此自 v1.1 起只统计【总上传量】，不再区分地址族。
package client

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// 数据结构
// ---------------------------------------------------------------------------

// Stats 是一次采样的结果。
type Stats struct {
	// Timestamp 是采样时刻。
	Timestamp time.Time `json:"ts"`
	// UpSpeed 是当前上传速度（字节/秒）。
	UpSpeed int64 `json:"up_speed"`
	// UpTotal 是客户端自启动以来的累计上传字节数。
	//
	// 这是客户端自己维护的单调递增计数器，
	// 调用方用两次采样的差值来计算增量。
	UpTotal int64 `json:"up_total"`
	// DlSpeed 是下载速度（字节/秒），仅用于展示。
	DlSpeed int64 `json:"dl_speed"`
	// PeerCount / TorrentCount 用于展示与排障。
	PeerCount    int `json:"peers"`
	TorrentCount int `json:"torrents"`
	// SpeedLimited 表示客户端当前是否处于限速状态。
	SpeedLimited bool `json:"speed_limited"`
	// CurrentUpLimit 是客户端当前生效的上传限速（字节/秒，-1 表示未查询/未知，0 表示不限速）。
	CurrentUpLimit int64 `json:"current_up_limit"`
	// Raw 可携带客户端原始响应，便于排障。
	Raw map[string]any `json:"-"`
}

// TotalUpSpeed 返回当前上传速度。
//
// 保留这个方法名是为了让调用方语义清晰（「总量」而非「某族」），
// 现在它就是 UpSpeed 本身。
func (s Stats) TotalUpSpeed() int64 { return s.UpSpeed }

// TotalUpBytes 返回累计上传字节数。
func (s Stats) TotalUpBytes() int64 { return s.UpTotal }

// ---------------------------------------------------------------------------
// 接口
// ---------------------------------------------------------------------------

// SpeedLimit 描述要施加的上传限速。
//
//	Total == -1 表示「不修改限速」
//	Total == 0  表示「取消限速（无限速）」
//	Total >  0  表示「限制为该字节/秒」
type SpeedLimit struct {
	Total int64 `json:"total"`
}

// NewNoopLimit 返回一个「不做任何修改」的限速指令。
func NewNoopLimit() SpeedLimit { return SpeedLimit{Total: -1} }

// NewUnlimited 返回一个「取消限速」的指令。
func NewUnlimited() SpeedLimit { return SpeedLimit{Total: 0} }

// Client 是所有下载器实现的统一接口。
type Client interface {
	// Name 返回配置中的客户端名。
	Name() string
	// Type 返回客户端类型（qbittorrent / transmission）。
	Type() string
	// Fetch 采集一次当前状态。
	Fetch(ctx context.Context) (Stats, error)
	// SetUploadLimit 下发上传限速。
	SetUploadLimit(ctx context.Context, limit SpeedLimit) error
}

// ---------------------------------------------------------------------------
// 带状态包装
// ---------------------------------------------------------------------------

// Managed 给任意 Client 附加健康状态与最近一次错误记录。
type Managed struct {
	Client
	mu       sync.RWMutex
	healthy  bool
	lastErr  error
	lastSeen time.Time
	// lastStats 缓存最近一次成功采样，用于客户端短暂不可用时保持曲线连续。
	lastStats Stats
}

// NewManaged 包装一个 Client。
func NewManaged(c Client) *Managed {
	return &Managed{Client: c, healthy: false}
}

// Fetch 采集状态并更新健康指标。
func (m *Managed) Fetch(ctx context.Context) (Stats, error) {
	st, err := m.Client.Fetch(ctx)
	m.mu.Lock()
	defer m.mu.Unlock()
	if err != nil {
		m.healthy = false
		m.lastErr = err
		return Stats{}, err
	}
	m.healthy = true
	m.lastErr = nil
	m.lastSeen = time.Now()
	m.lastStats = st
	return st, nil
}

// SetUploadLimit 下发限速并记录错误。
func (m *Managed) SetUploadLimit(ctx context.Context, limit SpeedLimit) error {
	err := m.Client.SetUploadLimit(ctx, limit)
	m.mu.Lock()
	defer m.mu.Unlock()
	if err != nil {
		m.healthy = false
		m.lastErr = err
	} else {
		m.healthy = true
		m.lastErr = nil
	}
	return err
}

// Healthy 返回客户端是否健康。
func (m *Managed) Healthy() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.healthy
}

// LastError 返回最近一次错误描述。
func (m *Managed) LastError() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.lastErr == nil {
		return ""
	}
	return m.lastErr.Error()
}

// LastSeen 返回最近一次成功采样的时刻。
func (m *Managed) LastSeen() time.Time {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.lastSeen
}

// LastStats 返回最近一次成功采样。
func (m *Managed) LastStats() Stats {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.lastStats
}

// ---------------------------------------------------------------------------
// 注册表
// ---------------------------------------------------------------------------

// Registry 保存全部已配置客户端，便于按名索引。
type Registry struct {
	mu      sync.RWMutex
	clients map[string]*Managed
	order   []string
}

// NewRegistry 创建空注册表。
func NewRegistry() *Registry {
	return &Registry{clients: map[string]*Managed{}}
}

// Add 注册一个客户端。
func (r *Registry) Add(m *Managed) {
	r.mu.Lock()
	defer r.mu.Unlock()
	key := strings.ToLower(m.Name())
	if _, exists := r.clients[key]; !exists {
		r.order = append(r.order, key)
	}
	r.clients[key] = m
}

// Get 按名查找客户端。
func (r *Registry) Get(name string) (*Managed, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	c, ok := r.clients[strings.ToLower(name)]
	return c, ok
}

// All 返回全部客户端，顺序与配置一致。
func (r *Registry) All() []*Managed {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Managed, 0, len(r.order))
	for _, k := range r.order {
		if c, ok := r.clients[k]; ok {
			out = append(out, c)
		}
	}
	return out
}

// Names 返回全部客户端名的有序列表。
func (r *Registry) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0, len(r.order))
	for _, k := range r.order {
		if c, ok := r.clients[k]; ok {
			out = append(out, c.Name())
		}
	}
	sort.Strings(out)
	return out
}

// Resolve 根据策略的客户端名单解析出实际要操作的客户端。
func (r *Registry) Resolve(names []string) []*Managed {
	if len(names) == 0 {
		return r.All()
	}
	all := r.All()
	wildcard := false
	for _, n := range names {
		if n == "*" {
			wildcard = true
			break
		}
	}
	if wildcard {
		return all
	}
	want := map[string]bool{}
	for _, n := range names {
		want[strings.ToLower(n)] = true
	}
	out := make([]*Managed, 0, len(names))
	for _, c := range all {
		if want[strings.ToLower(c.Name())] {
			out = append(out, c)
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// 工厂
// ---------------------------------------------------------------------------

// Options 是构造客户端所需的参数。
type Options struct {
	Name     string
	Type     string
	URL      string
	Username string
	Password string
	// APIKey 是 qBittorrent v5.2.0+ 的 WebUI API Key（形如 qbt_xxx）。
	// 非空时使用无状态 Bearer 认证，并忽略 Username / Password。
	// Transmission 不支持该机制，会被忽略。
	APIKey             string
	InsecureSkipVerify bool
	Timeout            time.Duration
	UploadLimitIPv4    int64
	UploadLimitIPv6    int64
}

// New 根据类型构造具体客户端实现。
func New(opt Options) (Client, error) {
	switch strings.ToLower(opt.Type) {
	case "qbittorrent":
		return NewQBittorrent(opt), nil
	case "transmission":
		return NewTransmission(opt), nil
	default:
		return nil, fmt.Errorf("不支持的客户端类型 %q", opt.Type)
	}
}
