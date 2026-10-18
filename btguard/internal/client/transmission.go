package client

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// Transmission 客户端
// ---------------------------------------------------------------------------
//
// 通过 RPC 接口（默认 /transmission/rpc）通信。首次请求会返回 409 并附带
// X-Transmission-Session-Id 头，后续请求必须回填该头，否则一直 409。
//
// 使用的 RPC 方法：
//
//	session-get          取 session 设置（含 speed-limit-up / speed-limit-up-enabled）
//	session-set          改 session 设置（限速）
//	session-stats        取全局速率与累计量（uploadedBytes / downloadSpeed 等）
//	torrent-get          取种子与 peer 详情（用于地址族归因）
//
// 关于 IPv4 / IPv6：
// Transmission 的 session-stats 只给「合计」上传速度与累计量。
// 分族归因同样依赖 torrent-get 的 peers 明细（peers 数组中带 address 字段）。
// 局限：Transmission 的 peer 明细缺少每个 peer 的上传速率，
// 因此本实现对速度按「各族的连接数占比」分摊，属于估算而非精确值。
// 真正精确的分族统计需要抓包（见 README 的说明与可选方案）。

// Transmission 实现 Client 接口。
type Transmission struct {
	name string
	doer *httpDoer

	sessMtx   sync.Mutex
	sessionID string

	verMtx   sync.Mutex
	version  string
	verAt    time.Time
	peerFlag bool

	tracker limitTracker
	// hasFamilyPreference 记录用户是否显式要求分族限速。
	prefersFamily bool

	// famMtx 保护「上一次有效的地址族比例」缓存。
	//
	// 动机：Transmission 只返回当前已连接 peer 的明细，
	// PT 保种时上传多为空闲、peers 为空，
	// 若此时直接判为「无法归类」，用户会看到流量长期停留在「未分类」。
	// 因此缓存最近一次有效比例，空闲时继续沿用。
	famMtx    sync.Mutex
	lastV4    int64
	lastV6    int64
	lastFamAt time.Time
}

// NewTransmission 构造 Transmission 客户端。
func NewTransmission(opt Options) *Transmission {
	return &Transmission{
		name:     opt.Name,
		doer:     newHTTPDoer(opt.URL, opt.Username, opt.Password, opt.Timeout, opt.InsecureSkipVerify, opt.Username != ""),
		verAt:    time.Time{},
		peerFlag: false,
	}
}

// Name 返回客户端名。
func (t *Transmission) Name() string { return t.name }

// Type 返回客户端类型。
func (t *Transmission) Type() string { return "transmission" }

// URL 返回配置中的 RPC 地址，用于界面展示。
func (t *Transmission) URL() string { return t.doer.base }

// rpcPath 返回 RPC 端点路径。
func (t *Transmission) rpcPath() string {
	base := t.doer.base
	if strings.HasSuffix(base, "/transmission/rpc") {
		return base
	}
	// 支持用户填写 http://host:9091/transmission/web 之类地址
	if i := strings.Index(base, "/transmission/"); i >= 0 {
		return base[:i] + "/transmission/rpc"
	}
	return base + "/transmission/rpc"
}

// ---------------------------------------------------------------------------
// RPC 调用
// ---------------------------------------------------------------------------

// rpcRequest 是一次 RPC 请求体。
type rpcRequest struct {
	Method    string `json:"method"`
	Arguments any    `json:"arguments,omitempty"`
	Tag       any    `json:"tag,omitempty"`
}

// rpcResponse 是一次 RPC 响应体。
type rpcResponse struct {
	Result    string          `json:"result"`
	Arguments json.RawMessage `json:"arguments"`
}

// call 执行一次 RPC，自动处理 409 与 session-id 续期。
func (t *Transmission) call(ctx context.Context, method string, args any, out any) error {
	payload := rpcRequest{Method: method, Arguments: args}

	return retry(ctx, 2, 400*time.Millisecond, func() error {
		t.sessMtx.Lock()
		sid := t.sessionID
		t.sessMtx.Unlock()

		headers := map[string]string{}
		if sid != "" {
			headers["X-Transmission-Session-Id"] = sid
		}

		blob, err := json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("序列化 Transmission 请求失败: %w", err)
		}

		data, code, err := t.doer.request(ctx, "POST", t.rpcPath(),
			strings.NewReader(string(blob)), "application/json", headers)
		if err != nil {
			return err
		}

		if code == 409 {
			// Transmission 在 409 的响应头里给出新的 Session-Id，需要重新发一次。
			newSID := t.fetchSessionID(ctx)
			if newSID == "" {
				// 兜底：极少数反代会把 id 写进 body。
				newSID = extractSessionID(data)
			}
			if newSID == "" {
				return fmt.Errorf("Transmission 返回 409 但未能取得 Session-Id（请确认地址指向 /transmission/rpc）")
			}
			t.sessMtx.Lock()
			t.sessionID = newSID
			t.sessMtx.Unlock()
			return fmt.Errorf("Transmission Session-Id 已更新，需要重试")
		}

		if code == 401 {
			return fmt.Errorf("Transmission 认证失败（HTTP 401）：请检查用户名/密码")
		}
		if code != 200 {
			return statusError("Transmission RPC "+method, code, data)
		}

		var resp rpcResponse
		if err := json.Unmarshal(data, &resp); err != nil {
			return fmt.Errorf("解析 Transmission 响应失败: %w", err)
		}
		if resp.Result != "success" {
			return fmt.Errorf("Transmission RPC %s 返回 %q", method, resp.Result)
		}
		if out != nil && len(resp.Arguments) > 0 {
			if err := json.Unmarshal(resp.Arguments, out); err != nil {
				return fmt.Errorf("解析 Transmission %s 参数失败: %w", method, err)
			}
		}
		return nil
	})
}

// fetchSessionID 单独发起一次请求以捕获 409 响应头中的 Session-Id。
func (t *Transmission) fetchSessionID(ctx context.Context) string {
	blob := `{"method":"session-get"}`
	_, code, sid := t.doer.requestWithHeaders(ctx, "POST", t.rpcPath(),
		strings.NewReader(blob), "application/json", nil,
		[]string{"X-Transmission-Session-Id"})
	if code == 409 {
		return sid
	}
	return sid
}

// extractSessionID 从响应体中尽力提取 session id（正常走响应头，这里是兜底）。
func extractSessionID(data []byte) string {
	s := strings.TrimSpace(string(data))
	// 某些反代会把 HTML 错误页返回，做一次简单查找。
	const marker = "X-Transmission-Session-Id"
	if i := strings.Index(s, marker); i >= 0 {
		rest := s[i+len(marker):]
		rest = strings.TrimLeft(rest, ": \r\n\t\"'")
		if j := strings.IndexAny(rest, "\r\n\"'<"); j > 0 {
			return strings.TrimSpace(rest[:j])
		}
		return strings.TrimSpace(rest)
	}
	return ""
}

// ---------------------------------------------------------------------------
// 采样
// ---------------------------------------------------------------------------

// sessionGetArgs 是 session-get 的返回。
type sessionGetArgs struct {
	Version             string `json:"version"`
	RPCVersion          int    `json:"rpc-version"`
	SpeedLimitUp        int64  `json:"speed-limit-up"`
	SpeedLimitUpEnabled bool   `json:"speed-limit-up-enabled"`
	AltSpeedEnabled     bool   `json:"alt-speed-enabled"`
	AltSpeedUp          int64  `json:"alt-speed-up"`
	DownloadDir         string `json:"download-dir"`
}

// sessionStatsArgs 是 session-stats 的返回。
type sessionStatsArgs struct {
	ActiveTorrentCount int   `json:"activeTorrentCount"`
	DownloadSpeed      int64 `json:"downloadSpeed"`
	UploadSpeed        int64 `json:"uploadSpeed"`
	PausedTorrentCount int   `json:"pausedTorrentCount"`
	TorrentCount       int   `json:"torrentCount"`
	CumulativeStats    struct {
		UploadedBytes   int64 `json:"uploadedBytes"`
		DownloadedBytes int64 `json:"downloadedBytes"`
		SecondsActive   int64 `json:"secondsActive"`
	} `json:"cumulative-stats"`
	CurrentStats struct {
		UploadedBytes int64 `json:"uploadedBytes"`
	} `json:"current-stats"`
}

// torrentGetArgs 是 torrent-get 的返回。
type torrentGetArgs struct {
	Torrents []struct {
		ID         int64   `json:"id"`
		Name       string  `json:"name"`
		UploadedEv int64   `json:"uploadedEver"`
		RateUp     int64   `json:"rateUpload"`
		Peers      []Tpeer `json:"peers"`
		PeersFrom  struct {
			FromCache   int `json:"fromCache"`
			FromDHT     int `json:"fromDht"`
			FromLPD     int `json:"fromLpd"`
			FromLTEP    int `json:"fromLtep"`
			FromPEX     int `json:"fromPex"`
			FromTracker int `json:"fromTracker"`
		} `json:"peersFrom"`
	} `json:"torrents"`
}

// Tpeer 是 Transmission 返回的单个 peer 信息。
type Tpeer struct {
	Address      string `json:"address"`
	Port         int    `json:"port"`
	ClientName   string `json:"clientName"`
	RateToPeer   int64  `json:"rateToPeer"`
	RateToClient int64  `json:"rateToClient"`
	Progress     any    `json:"progress"`
	IsEncrypted  bool   `json:"isEncrypted"`
	IsIncoming   bool   `json:"isIncoming"`
	PeerIsChoked bool   `json:"peerIsChoked"`
	FlagStr      string `json:"flagStr"`
}

// Fetch 采集一次 Transmission 状态。
func (t *Transmission) Fetch(ctx context.Context) (Stats, error) {
	var sess sessionGetArgs
	var stats sessionStatsArgs

	if err := t.call(ctx, "session-get", nil, &sess); err != nil {
		return Stats{}, err
	}
	if err := t.call(ctx, "session-stats", nil, &stats); err != nil {
		return Stats{}, err
	}

	// 记录版本
	t.verMtx.Lock()
	if sess.Version != "" {
		t.version = sess.Version
		t.verAt = time.Now()
	}
	t.verMtx.Unlock()

	upLimit := int64(0)
	if sess.SpeedLimitUpEnabled {
		upLimit = sess.SpeedLimitUp * 1000 // Transmission 使用 KB/s
	} else if sess.AltSpeedEnabled {
		upLimit = sess.AltSpeedUp * 1000
	}

	st := Stats{
		Timestamp:      time.Now(),
		UpSpeed:        stats.UploadSpeed,
		UpTotal:        stats.CumulativeStats.UploadedBytes,
		DlSpeed:        stats.DownloadSpeed,
		TorrentCount:   stats.TorrentCount,
		SpeedLimited:   sess.SpeedLimitUpEnabled || sess.AltSpeedEnabled,
		CurrentUpLimit: upLimit,
		Raw: map[string]any{
			"version":          sess.Version,
			"speed_limit_up":   sess.SpeedLimitUp,
			"alt_speed_up":     sess.AltSpeedUp,
			"alt_speed_active": sess.AltSpeedEnabled,
		},
	}

	// peer 连接数只用于展示。地址族统计已移除：
	// Transmission 不提供分族的累计量，只能按当前 peer 占比估算，
	// 而估算值会随 peer 增减跳动，导致虚假流量。
	if _, _, peers := t.splitByFamily(ctx); true {
		st.PeerCount = peers
	}

	return st, nil
}

// splitByFamily 统计当前已连接的 peer 数量。
//
// 返回 (unused v4 计数, unused v6 计数, 总 peer 数)。
// 地址族计数保留为 0 仅为兼容既有调用签名，已不再参与统计 ——
// 本工具现在只统计总上传量。
func (t *Transmission) splitByFamily(ctx context.Context) (int64, int64, int) {
	args := map[string]any{
		"fields": []string{"id", "peers", "peersFrom", "rateUpload", "uploadedEver"},
	}
	var res torrentGetArgs
	if err := t.call(ctx, "torrent-get", args, &res); err != nil {
		return 0, 0, 0
	}

	total := 0
	for _, tr := range res.Torrents {
		total += len(tr.Peers)
	}
	return 0, 0, total
}

// ---------------------------------------------------------------------------
// 限速
// ---------------------------------------------------------------------------

// SetUploadLimit 下发上传限速。
//
// Transmission 只有单一的全局上传限速（speed-limit-up，单位 KB/s）。
//
// limit.Total == 0 表示关闭上传限速，-1 表示不修改。
func (t *Transmission) SetUploadLimit(ctx context.Context, limit SpeedLimit) error {
	value := limit.Total
	if value < 0 {
		return nil
	}

	// Transmission 限速单位为 KB/s。向上取整，保证不超过指定字节速率。
	kb := (value + 999) / 1000
	enabled := value > 0

	args := map[string]any{
		"speed-limit-up":         kb,
		"speed-limit-up-enabled": enabled,
		// 关闭备用限速，避免两套限速互相干扰。
		"alt-speed-enabled": false,
	}
	if err := t.call(ctx, "session-set", args, nil); err != nil {
		return fmt.Errorf("设置 Transmission 上传限速失败: %w", err)
	}

	t.tracker.record(SpeedLimit{Total: value})
	return nil
}
