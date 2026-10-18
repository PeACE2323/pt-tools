package web

import (
	"net/http"
	"strings"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// ---------------------------------------------------------------------------
// 结构化配置接口
//
// 这些接口面向 Web UI 的图形化表单，避免用户手写 YAML。
// 与 /api/config/raw 的区别：
//   - raw  面向「高级用户」，直接读写 YAML 文本
//   - form 面向「普通用户」，读写结构化对象，由服务端负责序列化与校验
//
// 两者最终都落到同一份 config.yaml 上，改任意一边另一边都会看到。
// ---------------------------------------------------------------------------

// passwordMask 是回传给前端的密码占位符。
//
// 前端拿到掩码值后原样回传，服务端识别到掩码就沿用已保存的密码，
// 这样既能避免把密码明文发给浏览器，又不会因为前端未修改而清空密码。
const passwordMask = "\u0000__UNCHANGED__\u0000"

// clearToken 是前端用来表示「显式清空」的哨兵值。
//
// 语义区分（三者缺一不可）：
//
//	passwordMask  → 用户没动这个字段，沿用已保存的值
//	clearToken    → 用户主动清空，写入空值
//	其他任意字符串 → 用户填了新值，覆盖保存
//
// 如果只把「空串」当作「不变」，用户就永远无法清除已保存的密码或 API Key。
const clearToken = "\u0000__CLEAR__\u0000"

// isUnchanged 判断前端回传的是否为「未修改」。
//
// 注意：空串视为「未修改」，这是为了让用户没有展开高级选项时
// 不会误清空已有凭据。真正的清空必须显式传 clearToken。
func isUnchanged(v string) bool {
	return v == passwordMask || v == ""
}

// isClear 判断前端是否要求清空该字段。
func isClear(v string) bool {
	return v == clearToken
}

// resolveSecret 依据「未修改 / 清空 / 新值」三种语义决定最终写入的值。
func resolveSecret(submitted, existing string) string {
	switch {
	case isClear(submitted):
		return ""
	case isUnchanged(submitted):
		return existing
	default:
		return submitted
	}
}

// clientView 是客户端的对外视图（密码以掩码呈现）。
type clientView struct {
	Name               string `json:"name"`
	Type               string `json:"type"`
	URL                string `json:"url"`
	Username           string `json:"username"`
	Password           string `json:"password"`
	APIKey             string `json:"api_key"`
	Enabled            bool   `json:"enabled"`
	InsecureSkipVerify bool   `json:"insecure_skip_verify"`
	Timeout            string `json:"timeout"`

	// 以下为只读的派生信息，供界面展示
	AuthMode     string `json:"auth_mode"`
	HasPassword  bool   `json:"has_password"`
	HasAPIKey    bool   `json:"has_api_key"`
	APIKeyHint   string `json:"api_key_hint"`
	SupportsKey  bool   `json:"supports_api_key"`
	StatusText   string `json:"status_text,omitempty"`
	LastError    string `json:"last_error,omitempty"`
	Healthy      bool   `json:"healthy"`
	UpSpeed      int64  `json:"up_speed"`
	UpLimitNow   int64  `json:"current_up_limit"`
	VersionLabel string `json:"version,omitempty"`
}

// policyView 是策略的对外视图。
type policyView struct {
	Name    string   `json:"name"`
	Enabled bool     `json:"enabled"`
	Clients []string `json:"clients"`

	Window string `json:"window"`
	Align  string `json:"align"`

	ThresholdTotal string `json:"threshold_total"`

	LimitTotal string `json:"limit_total"`

	// 解除限速后要下发的速度。
	// 空字符串表示「未启用」—— 恢复时回到客户端的原始限速。
	ResumeLimitTotal string `json:"resume_limit_total"`

	ResumeOnWindowReset bool   `json:"resume_on_window_reset"`
	Cooldown            string `json:"cooldown"`
}

// formConfig 是图形化表单的完整配置载荷。
type formConfig struct {
	General  generalView  `json:"general"`
	Clients  []clientView `json:"clients"`
	Policies []policyView `json:"policies"`
	Notify   notifyView   `json:"notify"`
}

type generalView struct {
	Listen        string `json:"listen"`
	PollInterval  string `json:"poll_interval"`
	LogLevel      string `json:"log_level"`
	Timezone      string `json:"timezone"`
	HistoryPoints int    `json:"history_points"`
	AuthEnabled   bool   `json:"auth_enabled"`
	AuthUsername  string `json:"auth_username"`
	AuthPassword  string `json:"auth_password"`
}

type notifyView struct {
	Enabled  bool     `json:"enabled"`
	NotifyOn []string `json:"notify_on"`

	NtfyEnabled  bool   `json:"ntfy_enabled"`
	NtfyURL      string `json:"ntfy_url"`
	NtfyTopic    string `json:"ntfy_topic"`
	NtfyToken    string `json:"ntfy_token"`
	NtfyPriority string `json:"ntfy_priority"`

	WebhookEnabled bool              `json:"webhook_enabled"`
	WebhookURL     string            `json:"webhook_url"`
	WebhookMethod  string            `json:"webhook_method"`
	WebhookFormat  string            `json:"webhook_format"`
	WebhookHeaders map[string]string `json:"webhook_headers"`
}

// toView 把内部配置转换为对外视图。
func toView(cfg *config.Config) formConfig {
	out := formConfig{
		General: generalView{
			Listen:        cfg.Listen,
			PollInterval:  cfg.PollInterval.String(),
			LogLevel:      cfg.LogLevel,
			Timezone:      cfg.Timezone,
			HistoryPoints: cfg.HistoryPoints,
			AuthEnabled:   cfg.WebAuth.Enabled,
			AuthUsername:  cfg.WebAuth.Username,
			AuthPassword:  maskIfSet(cfg.WebAuth.Password),
		},
		Clients:  make([]clientView, 0, len(cfg.Clients)),
		Policies: make([]policyView, 0, len(cfg.Policies)),
		Notify: notifyView{
			Enabled:        cfg.Notify.Enabled,
			NotifyOn:       cfg.Notify.NotifyOn,
			NtfyEnabled:    cfg.Notify.Ntfy.Enabled,
			NtfyURL:        cfg.Notify.Ntfy.URL,
			NtfyTopic:      cfg.Notify.Ntfy.Topic,
			NtfyToken:      maskIfSet(cfg.Notify.Ntfy.Token),
			NtfyPriority:   cfg.Notify.Ntfy.Priority,
			WebhookEnabled: cfg.Notify.Webhook.Enabled,
			WebhookURL:     cfg.Notify.Webhook.URL,
			WebhookMethod:  cfg.Notify.Webhook.Method,
			WebhookFormat:  cfg.Notify.Webhook.Format,
			WebhookHeaders: cfg.Notify.Webhook.Headers,
		},
	}

	for _, c := range cfg.Clients {
		out.Clients = append(out.Clients, clientView{
			Name:               c.Name,
			Type:               c.Type,
			URL:                c.URL,
			Username:           c.Username,
			Password:           maskIfSet(c.Password),
			APIKey:             maskIfSet(c.APIKey),
			Enabled:            c.IsEnabled(),
			InsecureSkipVerify: c.InsecureSkipVerify,
			Timeout:            c.Timeout.String(),
			AuthMode:           c.AuthMode(),
			HasPassword:        c.Password != "",
			HasAPIKey:          c.APIKey != "",
			APIKeyHint:         keyHint(c.APIKey),
			SupportsKey:        c.Type == "qbittorrent",
		})
	}

	for _, p := range cfg.Policies {
		out.Policies = append(out.Policies, policyView{
			Name:                p.Name,
			Enabled:             p.IsEnabled(),
			Clients:             p.Clients,
			Window:              p.Window.String(),
			Align:               p.Align,
			ThresholdTotal:      formatBytesField(p.ThTotal()),
			LimitTotal:          formatBitrateField(p.LimTotal()),
			ResumeLimitTotal:    formatBitrateField(p.ResumeTotal()),
			ResumeOnWindowReset: p.ShouldResume(),
			Cooldown:            p.Cooldown.String(),
		})
	}
	return out
}

// maskIfSet 在值非空时返回掩码，否则返回空串。
func maskIfSet(v string) string {
	if strings.TrimSpace(v) == "" {
		return ""
	}
	return passwordMask
}

// keyHint 生成 API Key 的提示串（只露头尾，便于用户确认填对了哪一个）。
func keyHint(k string) string {
	k = strings.TrimSpace(k)
	if k == "" {
		return ""
	}
	if len(k) <= 12 {
		return strings.Repeat("*", len(k))
	}
	return k[:8] + "…" + k[len(k)-4:]
}

// formatBytesField 把字节数转成便于编辑的字符串；0 显示为空。
func formatBytesField(n int64) string {
	if n <= 0 {
		return ""
	}
	return humanBytes(n)
}

// formatBitrateField 把速率转成便于编辑的字符串；0 显示为空。
func formatBitrateField(n int64) string {
	if n <= 0 {
		return ""
	}
	return humanBytes(n)
}

// humanBytes 把字节数格式化为带单位的字符串（GiB/MiB 等）。
func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return itoa(n)
	}
	units := []string{"KiB", "MiB", "GiB", "TiB", "PiB"}
	val := float64(n)
	i := -1
	for val >= unit && i < len(units)-1 {
		val /= unit
		i++
	}
	// 去掉多余的小数位：整数就显示整数
	if val == float64(int64(val)) {
		return itoa(int64(val)) + units[i]
	}
	return trimFloat(val) + units[i]
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [24]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

func trimFloat(f float64) string {
	s := strings.TrimRight(strings.TrimRight(
		// 保留两位小数
		formatFloat2(f), "0"), ".")
	if s == "" {
		return "0"
	}
	return s
}

func formatFloat2(f float64) string {
	// 简单实现，避免引入 strconv 之外的分支
	whole := int64(f)
	frac := int64((f - float64(whole)) * 100)
	if frac < 0 {
		frac = -frac
	}
	if frac == 0 {
		return itoa(whole) + ".00"
	}
	if frac < 10 {
		return itoa(whole) + ".0" + itoa(frac)
	}
	return itoa(whole) + "." + itoa(frac)
}

// ---------------------------------------------------------------------------
// 保存：视图 → 配置
// ---------------------------------------------------------------------------

// applyView 把表单回传的数据合并进配置。
//
// 关键在于「掩码语义」：前端回传掩码或空串时，
// 表示用户没有修改该字段，应沿用已保存的值。
func applyView(cfg *config.Config, v formConfig) error {
	// --- 通用 ---
	if s := strings.TrimSpace(v.General.Listen); s != "" {
		cfg.Listen = s
	}
	if s := strings.TrimSpace(v.General.PollInterval); s != "" {
		d, err := config.ParseDuration(s)
		if err != nil {
			return apiError{Code: 400, Message: "采集间隔格式不正确: " + err.Error()}
		}
		cfg.PollInterval = d
	}
	if s := strings.TrimSpace(v.General.LogLevel); s != "" {
		cfg.LogLevel = s
	}
	cfg.Timezone = strings.TrimSpace(v.General.Timezone)
	if v.General.HistoryPoints > 0 {
		cfg.HistoryPoints = v.General.HistoryPoints
	}
	cfg.WebAuth.Enabled = v.General.AuthEnabled
	if s := strings.TrimSpace(v.General.AuthUsername); s != "" {
		cfg.WebAuth.Username = s
	}
	cfg.WebAuth.Password = resolveSecret(v.General.AuthPassword, cfg.WebAuth.Password)

	// --- 客户端 ---
	oldByName := map[string]config.ClientConfig{}
	for _, c := range cfg.Clients {
		oldByName[strings.ToLower(c.Name)] = c
	}

	clients := make([]config.ClientConfig, 0, len(v.Clients))
	for i, cv := range v.Clients {
		cv.Name = strings.TrimSpace(cv.Name)
		if cv.Name == "" {
			return apiError{Code: 400, Message: "第 " + itoa(int64(i+1)) + " 个客户端的名称不能为空"}
		}

		old, existed := oldByName[strings.ToLower(cv.Name)]

		timeout := config.Duration(0)
		if s := strings.TrimSpace(cv.Timeout); s != "" {
			d, err := config.ParseDuration(s)
			if err != nil {
				return apiError{Code: 400, Message: "客户端 " + cv.Name + " 的超时格式不正确: " + err.Error()}
			}
			timeout = d
		} else if existed {
			timeout = old.Timeout
		}

		cc := config.ClientConfig{
			Name:               cv.Name,
			Type:               strings.ToLower(strings.TrimSpace(cv.Type)),
			URL:                strings.TrimSpace(cv.URL),
			Username:           strings.TrimSpace(cv.Username),
			Enabled:            boolPtr(cv.Enabled),
			InsecureSkipVerify: cv.InsecureSkipVerify,
			Timeout:            timeout,
		}

		// 密码与 API Key 按「未修改 / 清空 / 新值」三态处理。
		// 新增的客户端没有旧值可沿用，existing 传空串即可。
		prevPass, prevKey := "", ""
		if existed {
			prevPass, prevKey = old.Password, old.APIKey
		}
		cc.Password = resolveSecret(cv.Password, prevPass)
		cc.APIKey = strings.TrimSpace(resolveSecret(cv.APIKey, prevKey))
		// Transmission 不支持 API Key
		if cc.Type != "qbittorrent" {
			cc.APIKey = ""
		}

		clients = append(clients, cc)
	}
	cfg.Clients = clients

	// --- 策略 ---
	policies := make([]config.PolicyConfig, 0, len(v.Policies))
	for i, pv := range v.Policies {
		pv.Name = strings.TrimSpace(pv.Name)
		if pv.Name == "" {
			return apiError{Code: 400, Message: "第 " + itoa(int64(i+1)) + " 条策略的名称不能为空"}
		}

		window := config.Duration(24 * 3600 * 1e9)
		if s := strings.TrimSpace(pv.Window); s != "" {
			d, err := config.ParseDuration(s)
			if err != nil {
				return apiError{Code: 400, Message: "策略 " + pv.Name + " 的周期格式不正确: " + err.Error()}
			}
			window = d
		}

		thTotal, err := parseBytesLoose(pv.ThresholdTotal)
		limTotal, err := parseBitrateLoose(pv.LimitTotal)
		if err != nil {
			return apiError{Code: 400, Message: "策略 " + pv.Name + " 的限速: " + err.Error()}
		}

		// 解除限速后的速度：留空表示不启用（回到客户端原始限速）。
		resTotal, err := parseBitrateLoose(pv.ResumeLimitTotal)
		if err != nil {
			return apiError{Code: 400, Message: "策略 " + pv.Name + " 的恢复速度: " + err.Error()}
		}

		cooldown := config.Duration(0)
		if s := strings.TrimSpace(pv.Cooldown); s != "" {
			d, err := config.ParseDuration(s)
			if err != nil {
				return apiError{Code: 400, Message: "策略 " + pv.Name + " 的冷却期格式不正确: " + err.Error()}
			}
			cooldown = d
		}

		align := strings.ToLower(strings.TrimSpace(pv.Align))
		if align == "" {
			align = "day"
		}

		policies = append(policies, config.PolicyConfig{
			Name:                pv.Name,
			Enabled:             boolPtr(pv.Enabled),
			Clients:             pv.Clients,
			Window:              window,
			Align:               align,
			ThresholdTotal:      config.Bytes(thTotal),
			LimitTotal:          config.Bitrate(limTotal),
			ResumeLimitTotal:    config.Bitrate(resTotal),
			ResumeOnWindowReset: boolPtr(pv.ResumeOnWindowReset),
			Cooldown:            cooldown,
		})
	}
	cfg.Policies = policies

	// --- 通知 ---
	cfg.Notify.Enabled = v.Notify.Enabled
	if v.Notify.NotifyOn != nil {
		cfg.Notify.NotifyOn = v.Notify.NotifyOn
	}
	cfg.Notify.Ntfy.Enabled = v.Notify.NtfyEnabled
	if s := strings.TrimSpace(v.Notify.NtfyURL); s != "" {
		cfg.Notify.Ntfy.URL = s
	}
	cfg.Notify.Ntfy.Topic = strings.TrimSpace(v.Notify.NtfyTopic)
	cfg.Notify.Ntfy.Token = resolveSecret(v.Notify.NtfyToken, cfg.Notify.Ntfy.Token)
	if s := strings.TrimSpace(v.Notify.NtfyPriority); s != "" {
		cfg.Notify.Ntfy.Priority = s
	}
	cfg.Notify.Webhook.Enabled = v.Notify.WebhookEnabled
	cfg.Notify.Webhook.URL = strings.TrimSpace(v.Notify.WebhookURL)
	if s := strings.TrimSpace(v.Notify.WebhookMethod); s != "" {
		cfg.Notify.Webhook.Method = strings.ToUpper(s)
	}
	if s := strings.TrimSpace(v.Notify.WebhookFormat); s != "" {
		cfg.Notify.Webhook.Format = strings.ToLower(s)
	}
	cfg.Notify.Webhook.Headers = v.Notify.WebhookHeaders

	return cfg.Validate()
}

func boolPtr(v bool) *bool { return &v }

// parseBytesLoose 解析容量字符串；空串视为 0。
func parseBytesLoose(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, nil
	}
	return config.ParseBytes(s)
}

// parseBitrateLoose 解析速率字符串；空串视为 0。
func parseBitrateLoose(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, nil
	}
	return config.ParseBitrate(s)
}

// ---------------------------------------------------------------------------
// 处理函数
// ---------------------------------------------------------------------------

// handleFormConfig 读写结构化配置。
func (s *Server) handleFormConfig(w http.ResponseWriter, r *http.Request) error {
	cfg := s.deps.Config()
	if cfg == nil {
		return apiError{Code: 500, Message: "配置未初始化"}
	}

	switch r.Method {
	case http.MethodGet:
		view := toView(cfg)
		// 附加运行状态，让表单能显示「已连接 / 异常」等实时信息
		s.enrichClientViews(&view)
		return writeJSON(w, view)

	case http.MethodPost, http.MethodPut:
		var body formConfig
		if err := decodeJSON(r, &body); err != nil {
			return err
		}
		// 在副本上修改，校验通过后才替换，避免半途失败留下脏配置
		next := *cfg
		next.Clients = append([]config.ClientConfig(nil), cfg.Clients...)
		next.Policies = append([]config.PolicyConfig(nil), cfg.Policies...)
		if err := applyView(&next, body); err != nil {
			return err
		}
		if s.deps.UpdateConfig == nil {
			return apiError{Code: 500, Message: "配置更新未启用"}
		}
		if err := s.deps.UpdateConfig(&next); err != nil {
			return apiError{Code: 400, Message: err.Error()}
		}
		view := toView(s.deps.Config())
		s.enrichClientViews(&view)
		return writeJSON(w, map[string]any{
			"ok":      true,
			"message": "配置已保存并生效",
			"config":  view,
		})

	default:
		return apiError{Code: http.StatusMethodNotAllowed, Message: "不支持的方法"}
	}
}

// enrichClientViews 把运行状态合并进客户端视图。
func (s *Server) enrichClientViews(v *formConfig) {
	if s.deps.monitor() == nil {
		return
	}
	for i := range v.Clients {
		name := v.Clients[i].Name
		snap, ok := s.deps.monitor().Snapshot(name)
		if !ok {
			v.Clients[i].StatusText = "等待首次采集"
			continue
		}
		v.Clients[i].Healthy = snap.Healthy
		v.Clients[i].LastError = snap.LastError
		v.Clients[i].UpLimitNow = snap.CurrentUpLimit
		v.Clients[i].UpSpeed = snap.UpSpeed
		if snap.Healthy {
			v.Clients[i].StatusText = "正常"
		} else {
			v.Clients[i].StatusText = "异常"
		}
	}
}
