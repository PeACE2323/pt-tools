package client

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// qBittorrent 客户端
// ---------------------------------------------------------------------------
//
// 依赖 WebUI API v2：
//
//	POST /api/v2/auth/login                    登录，返回 SID Cookie
//	GET  /api/v2/transfer/info                 全局传输信息（含 up_info_speed / up_info_data）
//	POST /api/v2/transfer/setUploadLimit       设置全局上传限速（字节/秒，0 表示无限）
//	GET  /api/v2/torrents/info                  种子列表（用于统计连接数与地址族分布）
//	GET  /api/v2/app/version                    版本号
//
// 关于 IPv4 / IPv6 统计：
// qBittorrent 的 WebUI API 只暴露「合计」上传速度与累计量，不区分地址族。
// 因此本实现通过 /api/v2/torrents/info 的 peer 分布做族归因估算：
// 读取每个种子的连接数与已连接 peer 信息，按 peer 地址推断族。
// 若接口未提供足够信息（多数版本如此），则退化为：
//   - 若配置中只关心一个族，把全部流量归到该族；
//   - 注：v1.1 起不再区分地址族，只统计总上传量。
//
// 同时，qBittorrent 4.6+ 支持 preferences 中的分族限速（ipv4/ipv6 上传限速），
// 本实现会在可用时通过 /api/v2/app/setPreferences 下发分族限速。

// QBittorrent 实现 Client 接口。
type QBittorrent struct {
	name     string
	doer     *httpDoer
	prefsMtx sync.Mutex

	// apiKey 非空时使用 qBittorrent v5.2.0+ 的无状态 Bearer 认证，
	// 完全跳过登录/登出流程。
	apiKey string

	// 登录状态（仅在未使用 API Key 时生效）
	authMtx  sync.Mutex
	loggedIn bool
	lastAuth time.Time

	// 连接信息缓存
	verMtx    sync.Mutex
	version   string
	versionAt time.Time

	tracker limitTracker
}

// NewQBittorrent 构造 qBittorrent 客户端。
//
// 认证方式自动选择：
//   - opt.APIKey 非空 → 使用 v5.2.0+ 的 Bearer API Key（无状态）
//   - 否则           → 使用传统的用户名密码登录（Cookie 会话）
func NewQBittorrent(opt Options) *QBittorrent {
	apiKey := strings.TrimSpace(opt.APIKey)
	doer := newHTTPDoer(opt.URL, opt.Username, opt.Password, opt.Timeout, opt.InsecureSkipVerify, false)
	if apiKey != "" {
		// 挂到 doer 上，使每个请求都带上 Authorization: Bearer <key>。
		doer.bearer = apiKey
	}
	return &QBittorrent{
		name:   opt.Name,
		doer:   doer,
		apiKey: apiKey,
	}
}

// Name 返回客户端名。
func (q *QBittorrent) Name() string { return q.name }

// Type 返回客户端类型。
func (q *QBittorrent) Type() string { return "qbittorrent" }

// URL 返回配置中的 WebUI 地址，用于界面展示。
func (q *QBittorrent) URL() string { return q.doer.base }

// UsesAPIKey 返回本客户端是否使用 API Key 认证。
func (q *QBittorrent) UsesAPIKey() bool { return q.apiKey != "" }

// login 执行登录以获取 SID cookie。
//
// 使用 API Key 时本方法不做任何事——API Key 是无状态认证，
// 官方明确说明其不能用于 auth/login 与 auth/logout 端点。
func (q *QBittorrent) login(ctx context.Context) error {
	if q.apiKey != "" {
		return nil
	}

	q.authMtx.Lock()
	defer q.authMtx.Unlock()

	if q.loggedIn && time.Since(q.lastAuth) < 20*time.Minute {
		return nil
	}

	// 既没有 API Key 也没有账号密码：直接给出可操作的提示，
	// 而不是发一个必然失败的登录请求再报 HTTP 401。
	if q.doer.user == "" && q.doer.pass == "" {
		return fmt.Errorf("客户端 %s 未配置认证信息：请在 WebUI 的「配置 → 下载器」中填写 "+
			"API Key（qBittorrent 5.2.0+）或用户名密码", q.name)
	}

	form := url.Values{}
	form.Set("username", q.doer.user)
	form.Set("password", q.doer.pass)
	// 允许 qBittorrent 的「对本地主机跳过认证」以及 IP 封禁场景下的正常流程。
	form.Set("referer", q.doer.base)

	data, code, err := q.doer.postForm(ctx, "/api/v2/auth/login", form, nil)
	if err != nil {
		return fmt.Errorf("登录 qBittorrent 失败: %w", err)
	}
	if code == 403 {
		return fmt.Errorf("登录 qBittorrent 被拒绝（HTTP 403）：连续失败次数过多，请在 WebUI 中解除 IP 封禁，或检查账号密码")
	}
	if code == 401 {
		return fmt.Errorf("登录 qBittorrent 失败（HTTP 401）：用户名或密码不正确。" +
			"若你的 qBittorrent 是 5.2.0 及以上版本，建议改用 API Key 认证（设置 → WebUI → API Key），" +
			"可避免反复登录触发 IP 封禁")
	}
	if code != 200 {
		return statusError("登录 qBittorrent", code, data)
	}

	body := strings.TrimSpace(strings.ToLower(string(data)))
	if body != "ok." && body != "ok" {
		if strings.Contains(body, "banned") {
			return fmt.Errorf("登录 qBittorrent 失败：IP 已被临时封禁，请稍后再试或在 WebUI 中解除")
		}
		if strings.Contains(body, "fail") {
			return fmt.Errorf("登录 qBittorrent 失败：用户名或密码错误")
		}
		return fmt.Errorf("登录 qBittorrent 返回未知结果: %q", strings.TrimSpace(string(data)))
	}

	q.loggedIn = true
	q.lastAuth = time.Now()
	return nil
}

// invalidateAuth 让下次请求重新登录（遇到 403 时调用）。
func (q *QBittorrent) invalidateAuth() {
	q.authMtx.Lock()
	q.loggedIn = false
	q.authMtx.Unlock()
}

// apiGet 带自动登录重试的 GET。
func (q *QBittorrent) apiGet(ctx context.Context, path string) ([]byte, error) {
	if err := q.login(ctx); err != nil {
		return nil, err
	}
	data, code, err := q.doer.get(ctx, path)
	if err != nil {
		return nil, err
	}
	if code == 403 || code == 401 {
		// 使用 API Key 时不存在「重新登录」这一步，
		// 401/403 只能说明 Key 无效或权限不足，直接给出明确提示。
		if q.apiKey != "" {
			return nil, fmt.Errorf("qBittorrent 拒绝访问（HTTP %d）：API Key 无效或已被轮换。"+
				"请在 qBittorrent 的「设置 → WebUI → API Key」中重新生成，并更新本工具的配置", code)
		}
		q.invalidateAuth()
		if err := q.login(ctx); err != nil {
			return nil, err
		}
		data, code, err = q.doer.get(ctx, path)
		if err != nil {
			return nil, err
		}
	}
	if code != 200 {
		return nil, statusError("qBittorrent GET "+path, code, data)
	}
	return data, nil
}

// apiPost 带自动登录重试的 POST（表单）。
func (q *QBittorrent) apiPost(ctx context.Context, path string, form url.Values) ([]byte, error) {
	if err := q.login(ctx); err != nil {
		return nil, err
	}
	data, code, err := q.doer.postForm(ctx, path, form, nil)
	if err != nil {
		return nil, err
	}
	if code == 403 || code == 401 {
		if q.apiKey != "" {
			return nil, fmt.Errorf("qBittorrent 拒绝访问（HTTP %d）：API Key 无效或已被轮换。"+
				"请在 qBittorrent 的「设置 → WebUI → API Key」中重新生成，并更新本工具的配置", code)
		}
		q.invalidateAuth()
		if err := q.login(ctx); err != nil {
			return nil, err
		}
		data, code, err = q.doer.postForm(ctx, path, form, nil)
		if err != nil {
			return nil, err
		}
	}
	if code != 200 {
		return nil, statusError("qBittorrent POST "+path, code, data)
	}
	return data, nil
}

// ---------------------------------------------------------------------------
// 版本与能力探测
// ---------------------------------------------------------------------------

// detectVersion 查询 qBittorrent 版本号并缓存 10 分钟。
func (q *QBittorrent) detectVersion(ctx context.Context) string {
	q.verMtx.Lock()
	if q.version != "" && time.Since(q.versionAt) < 10*time.Minute {
		v := q.version
		q.verMtx.Unlock()
		return v
	}
	q.verMtx.Unlock()

	data, err := q.apiGet(ctx, "/api/v2/app/version")
	if err != nil {
		return ""
	}
	v := strings.TrimSpace(string(data))
	v = strings.TrimPrefix(v, "v")

	q.verMtx.Lock()
	q.version = v
	q.versionAt = time.Now()
	q.verMtx.Unlock()
	return v
}

// CheckAuthOnStartup 在启动阶段做一次认证能力自检，返回可供日志输出的提示。
//
// 主要覆盖两种容易踩坑的情况：
//   - 填了 API Key 但 qBittorrent 版本低于 5.2.0（该版本没有这个机制）
//   - 只填了用户名密码，但客户端已升级到 5.2.0+ 且启用了 API Key 认证
//
// 返回的字符串为空表示一切正常；否则是可读的警告信息。
// 本方法不返回错误，因为它只用于提示，不应阻断启动。
func (q *QBittorrent) CheckAuthOnStartup(ctx context.Context) string {
	v := q.detectVersion(ctx)
	if v == "" {
		// 版本都取不到，说明连不通或认证失败，交给常规错误路径处理。
		return ""
	}

	const apiKeySinceMaj, apiKeySinceMin = 5, 2
	hasAPIKeySupport := versionAtLeast(v, apiKeySinceMaj, apiKeySinceMin, 0)

	if q.apiKey != "" && !hasAPIKeySupport {
		return fmt.Sprintf("客户端 %s（qBittorrent %s）不支持 API Key 认证，"+
			"该功能需要 5.2.0 及以上版本。请改用用户名密码，或升级 qBittorrent。",
			q.name, v)
	}
	return ""
}

// versionAtLeast 比较语义化版本号，判断 v >= (maj,min,pat)。
func versionAtLeast(v string, maj, min, pat int) bool {
	// 去掉 "-beta1" 之类后缀
	if i := strings.IndexAny(v, "-+ "); i >= 0 {
		v = v[:i]
	}
	parts := strings.Split(v, ".")
	get := func(i int) int {
		if i >= len(parts) {
			return 0
		}
		n, err := strconv.Atoi(strings.TrimSpace(parts[i]))
		if err != nil {
			return 0
		}
		return n
	}
	a, b, c := get(0), get(1), get(2)
	if a != maj {
		return a > maj
	}
	if b != min {
		return b > min
	}
	return c >= pat
}

// ---------------------------------------------------------------------------
// 采样
// ---------------------------------------------------------------------------

// transferInfo 是 /api/v2/transfer/info 的响应子集。
type transferInfo struct {
	UpInfoSpeed      int64  `json:"up_info_speed"`
	UpInfoData       int64  `json:"up_info_data"`
	DlInfoSpeed      int64  `json:"dl_info_speed"`
	DlInfoData       int64  `json:"dl_info_data"`
	UpRateLimit      int64  `json:"up_rate_limit"`
	DlRateLimit      int64  `json:"dl_rate_limit"`
	ConnectionStatus string `json:"connection_status"`
	GlobalRatio      any    `json:"global_ratio"`
}

// mainData 是 /api/v2/sync/maindata 的响应子集。
type mainData struct {
	ServerState struct {
		UpInfoSpeed   int64 `json:"up_info_speed"`
		UpInfoData    int64 `json:"up_info_data"`
		DlInfoSpeed   int64 `json:"dl_info_speed"`
		UpRateLimit   int64 `json:"up_rate_limit"`
		AlltimeUL     int64 `json:"alltime_ul"`
		TotalPeerConn int   `json:"total_peer_connections"`
	} `json:"server_state"`
	Torrents map[string]struct {
		UpSpeed      int64 `json:"upspeed"`
		UpData       int64 `json:"uploaded"`
		NumLeechs    int   `json:"num_leechs"`
		NumSeeds     int   `json:"num_seeds"`
		NumComplete  int   `json:"num_complete"`
		NumIncomplet int   `json:"num_incomplete"`
	} `json:"torrents"`
	// 部分版本提供 peers 明细
	Peers map[string]map[string]struct {
		UpSpeed int64  `json:"up_speed"`
		Country string `json:"country"`
		IP      string `json:"ip"`
		Flags   string `json:"flags"`
	} `json:"peers"`
}

// Fetch 采集一次 qBittorrent 状态。
func (q *QBittorrent) Fetch(ctx context.Context) (Stats, error) {
	var info transferInfo
	var md mainData
	var infoOK, mdOK bool
	var infoErr error

	if err := retry(ctx, 2, 300*time.Millisecond, func() error {
		data, err := q.apiGet(ctx, "/api/v2/transfer/info")
		if err != nil {
			return err
		}
		return json.Unmarshal(data, &info)
	}); err == nil {
		infoOK = true
	} else {
		infoErr = err
	}

	// maindata 提供 peer 明细，用于地址族归因；失败不影响主流程。
	if data, err := q.apiGet(ctx, "/api/v2/sync/maindata"); err == nil {
		if err := json.Unmarshal(data, &md); err == nil {
			mdOK = true
		}
	}

	if !infoOK && !mdOK {
		// 尽量把底层原因透出来。认证类错误（Key 无效 / 密码错误）
		// 是最常见的失败原因，直接吞掉会让用户排查困难。
		if infoErr != nil {
			return Stats{}, fmt.Errorf("无法获取 qBittorrent 传输信息：%w", infoErr)
		}
		return Stats{}, fmt.Errorf("无法获取 qBittorrent 传输信息（transfer/info 与 sync/maindata 均失败）")
	}

	upSpeed := info.UpInfoSpeed
	upData := info.UpInfoData
	dlSpeed := info.DlInfoSpeed
	upLimit := info.UpRateLimit
	if upSpeed == 0 && mdOK {
		upSpeed = md.ServerState.UpInfoSpeed
		upData = md.ServerState.UpInfoData
		dlSpeed = md.ServerState.DlInfoSpeed
		upLimit = md.ServerState.UpRateLimit
	}

	st := Stats{
		Timestamp:      time.Now(),
		UpSpeed:        upSpeed,
		UpTotal:        upData,
		DlSpeed:        dlSpeed,
		PeerCount:      md.ServerState.TotalPeerConn,
		TorrentCount:   len(md.Torrents),
		SpeedLimited:   upLimit > 0,
		CurrentUpLimit: upLimit,
		Raw:            map[string]any{"version": q.detectVersion(ctx)},
	}

	return st, nil
}

// ---------------------------------------------------------------------------
// 限速
// ---------------------------------------------------------------------------

// SetUploadLimit 下发全局上传限速。
func (q *QBittorrent) SetUploadLimit(ctx context.Context, limit SpeedLimit) error {
	value := limit.Total
	if value < 0 {
		// -1 表示「不修改」。
		return nil
	}

	form := url.Values{}
	form.Set("limit", strconv.FormatInt(value, 10))
	if _, err := q.apiPost(ctx, "/api/v2/transfer/setUploadLimit", form); err != nil {
		return fmt.Errorf("设置 qBittorrent 上传限速失败: %w", err)
	}

	q.tracker.record(SpeedLimit{Total: value})
	return nil
}

// maxInt64 返回较大值。
func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
