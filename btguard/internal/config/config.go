// Package config 负责加载、校验并热更新 Traffic Guard 的配置文件。
//
// 配置来源优先级（从低到高）：
//  1. 内置默认值
//  2. config.yaml
//  3. 环境变量（BTG_ 前缀）
//
// 这样 Unraid 用户既可以直接编辑挂载出来的 yaml，也可以用环境变量快速覆盖。
package config

import (
	"bytes"
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// ---------------------------------------------------------------------------
// 顶层结构
// ---------------------------------------------------------------------------

// Config 是整个应用的配置树。
type Config struct {
	// Listen 是 Web UI / API 的监听地址，例如 ":8080"。
	Listen string `yaml:"listen"`
	// DataDir 用于存放历史流量数据库与运行时状态。
	DataDir string `yaml:"data_dir"`
	// PollInterval 是采集一次客户端上传速率的间隔。
	PollInterval Duration `yaml:"poll_interval"`
	// LogLevel 取值 debug / info / warn / error。
	LogLevel string `yaml:"log_level"`
	// Timezone 用于展示与周期边界计算，留空则用容器本地时区。
	Timezone string `yaml:"timezone"`
	// HistoryPoints 是内存中保留的实时曲线点数（每个点一条采样）。
	HistoryPoints int `yaml:"history_points"`

	// Clients 是需要监控的下载器列表。
	Clients []ClientConfig `yaml:"clients"`
	// Policies 是限速策略列表，按顺序独立求值。
	Policies []PolicyConfig `yaml:"policies"`

	// Notify 是通知渠道配置。
	Notify NotifyConfig `yaml:"notify"`
	// WebAuth 保护 Web UI。
	WebAuth WebAuthConfig `yaml:"web_auth"`
}

// Bytes 包装 int64，额外支持 "500GiB" / "1.5TB" / "1048576" 这类 YAML 写法。
// 用于阈值字段，保持配置文件可读性。
type Bytes int64

// Int64 返回底层值。
func (b Bytes) Int64() int64 { return int64(b) }

func (b Bytes) String() string { return strconv.FormatInt(int64(b), 10) }

// UnmarshalYAML 实现 yaml.Unmarshaler。
func (b *Bytes) UnmarshalYAML(value *yaml.Node) error {
	s := strings.TrimSpace(value.Value)
	if s == "" {
		*b = 0
		return nil
	}
	n, err := ParseBytes(s)
	if err != nil {
		return err
	}
	*b = Bytes(n)
	return nil
}

// MarshalYAML 以整数形式回写，保证再次读取时语义不变。
func (b Bytes) MarshalYAML() (any, error) { return int64(b), nil }

// Bitrate 包装 int64，额外支持 "1M" / "512K" / "1048576" 这类 YAML 写法。
// 用于限速字段（单位字节/秒）。
type Bitrate int64

// Int64 返回底层值。
func (b Bitrate) Int64() int64 { return int64(b) }

func (b Bitrate) String() string { return strconv.FormatInt(int64(b), 10) }

// UnmarshalYAML 实现 yaml.Unmarshaler。
func (b *Bitrate) UnmarshalYAML(value *yaml.Node) error {
	s := strings.TrimSpace(value.Value)
	if s == "" {
		*b = 0
		return nil
	}
	n, err := ParseBitrate(s)
	if err != nil {
		return err
	}
	*b = Bitrate(n)
	return nil
}

// MarshalYAML 以整数形式回写。
func (b Bitrate) MarshalYAML() (any, error) { return int64(b), nil }

// ---------------------------------------------------------------------------
// 客户端
// ---------------------------------------------------------------------------

// ClientConfig 描述一个 qBittorrent 或 Transmission 实例。
type ClientConfig struct {
	// Name 是用户可见的唯一标识，用于策略匹配与展示。
	Name string `yaml:"name"`
	// Type 取值 qbittorrent 或 transmission。
	Type string `yaml:"type"`
	// URL 是 WebUI / RPC 根地址。
	// qBittorrent: http://192.168.1.10:8080
	// Transmission: http://192.168.1.10:9091
	URL string `yaml:"url"`
	// Username / Password 用于 qBittorrent 登录或 Transmission Basic Auth。
	Username string `yaml:"username"`
	Password string `yaml:"password"`
	// APIKey 是 qBittorrent v5.2.0+ 的 WebUI API Key（形如 qbt_xxxxxxxx）。
	//
	// 填写后走无状态的 Bearer 认证，不再执行登录/登出流程，
	// 且优先于用户名密码。留空则回退到用户名密码登录。
	// Transmission 不支持该机制，填了会被忽略。
	APIKey string `yaml:"api_key"`
	// Enabled 为 false 时跳过该客户端。
	Enabled *bool `yaml:"enabled"`
	// InsecureSkipVerify 跳过 TLS 证书校验（自签名 NAS 常见）。
	InsecureSkipVerify bool `yaml:"insecure_skip_verify"`
	// Timeout 是单次 API 调用的超时。
	Timeout Duration `yaml:"timeout"`
}

// IsEnabled 返回该客户端是否启用（默认启用）。
func (c ClientConfig) IsEnabled() bool { return c.Enabled == nil || *c.Enabled }

// UsesAPIKey 返回该客户端是否配置了 API Key 认证。
//
// 只有 qBittorrent 支持 API Key；Transmission 即便误填也走用户名密码。
func (c ClientConfig) UsesAPIKey() bool {
	return c.Type == "qbittorrent" && strings.TrimSpace(c.APIKey) != ""
}

// AuthMode 返回认证方式的可读描述，用于日志与界面展示。
func (c ClientConfig) AuthMode() string {
	switch {
	case c.UsesAPIKey():
		return "API Key"
	case c.Username != "" || c.Password != "":
		return "用户名密码"
	default:
		return "无认证"
	}
}

// ---------------------------------------------------------------------------
// 策略
// ---------------------------------------------------------------------------

// PolicyConfig 描述一条「流量达到阈值即限速」的规则。
type PolicyConfig struct {
	Name    string `yaml:"name"`
	Enabled *bool  `yaml:"enabled"`

	// Clients 是要施加限速的客户端名；为空或含 "*" 表示全部。
	Clients []string `yaml:"clients"`

	// Window 是统计周期，例如 "24h"、"1d"、"7d"、"30d"。
	// 支持的特殊写法见 ParseWindow 的说明。
	Window Duration `yaml:"window"`
	// Align 决定周期边界如何对齐：rolling / hour / day / week / month。
	// rolling（默认）表示从进程启动或上次重置起算的滑动周期。
	Align string `yaml:"align"`

	// --- 触发条件 ---
	// ThresholdTotal 是周期内的上传总量阈值（字节），必须大于 0。
	ThresholdTotal Bytes `yaml:"threshold_total"`

	// --- 限速动作 ---
	// LimitTotal 是触发后设置的上传限速（字节/秒），0 表示完全不限速。
	LimitTotal Bitrate `yaml:"limit_total"`

	// ResumeOnWindowReset 为 true 时，进入新周期自动恢复到客户端原始限速。
	ResumeOnWindowReset *bool `yaml:"resume_on_window_reset"`

	// --- 解除限速后的速度 ---
	//
	// ResumeLimitTotal 指定「解除限速后」要下发的上传限速（字节/秒）。
	//
	//	-1（默认）不清零，语义为「恢复到触发前的原始限速」
	//	 0        完全不限速（显式放开）
	//	>0       恢复到这个指定速度
	//
	// 典型用法：周期结束后不想立刻全速放开（怕又瞬间冲满额度），
	// 而是回到一个中间速度，例如 5 MiB/s。
	//
	// 若设为 >0，则优先采用它，而不再回到客户端的原始限速。
	ResumeLimitTotal Bitrate `yaml:"resume_limit_total"`

	// Cooldown 触发限速后至少保持多久才允许解除，避免临界抖动。
	Cooldown Duration `yaml:"cooldown"`
}

// IsEnabled 返回该策略是否启用（默认启用）。
func (p PolicyConfig) IsEnabled() bool { return p.Enabled == nil || *p.Enabled }

// ShouldResume 返回周期结束后是否自动解除限速（默认 true）。
func (p PolicyConfig) ShouldResume() bool {
	return p.ResumeOnWindowReset == nil || *p.ResumeOnWindowReset
}

// HasResumeLimit 返回是否配置了自定义的恢复速度。
func (p PolicyConfig) HasResumeLimit() bool {
	return p.ResumeLimitTotal > 0
}

// ResumeTotal / ResumeIPv4 / ResumeIPv6 返回自定义恢复速度。
func (p PolicyConfig) ResumeTotal() int64 { return int64(p.ResumeLimitTotal) }

// --- 便于调用方使用的 int64 访问器（Bytes / Bitrate 只是 YAML 表现层包装）---

// ThTotal 返回阈值的字节数。
func (p PolicyConfig) ThTotal() int64 { return int64(p.ThresholdTotal) }

// LimTotal 返回限速的字节/秒值。
func (p PolicyConfig) LimTotal() int64 { return int64(p.LimitTotal) }

// HasThreshold 返回是否配置了阈值。
func (p PolicyConfig) HasThreshold() bool { return p.ThTotal() > 0 }

// AppliesTo 判断该策略是否作用于指定客户端。
func (p PolicyConfig) AppliesTo(clientName string) bool {
	if len(p.Clients) == 0 {
		return true
	}
	for _, n := range p.Clients {
		if n == "*" || strings.EqualFold(n, clientName) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// 通知
// ---------------------------------------------------------------------------

// NotifyConfig 汇总所有通知渠道。
type NotifyConfig struct {
	Enabled bool `yaml:"enabled"`
	// NotifyOn 取值 speedup / resume / error，默认全部。
	NotifyOn []string `yaml:"notify_on"`

	Ntfy    NtfyConfig    `yaml:"ntfy"`
	Webhook WebhookConfig `yaml:"webhook"`
}

// NtfyConfig 是 ntfy.sh 或自建 ntfy 服务的推送配置。
type NtfyConfig struct {
	Enabled  bool   `yaml:"enabled"`
	URL      string `yaml:"url"`      // 例如 https://ntfy.sh
	Topic    string `yaml:"topic"`    // 例如 bt-traffic-guard
	Token    string `yaml:"token"`    // 可选 Bearer Token
	Priority string `yaml:"priority"` // min/low/default/high/urgent
}

// WebhookConfig 是通用 JSON Webhook（可对接 Discord / Slack / 自建服务）。
type WebhookConfig struct {
	Enabled bool              `yaml:"enabled"`
	URL     string            `yaml:"url"`
	Method  string            `yaml:"method"` // 默认 POST
	Headers map[string]string `yaml:"headers"`
	// Format 取值 generic / discord / slack。
	Format string `yaml:"format"`
}

// ShouldNotify 判断某个事件类型是否需要推送。
func (n NotifyConfig) ShouldNotify(event string) bool {
	if !n.Enabled {
		return false
	}
	if len(n.NotifyOn) == 0 {
		return true
	}
	for _, e := range n.NotifyOn {
		if strings.EqualFold(e, event) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Web 认证
// ---------------------------------------------------------------------------

// WebAuthConfig 控制 Web UI 的访问控制。
type WebAuthConfig struct {
	Enabled  bool   `yaml:"enabled"`
	Username string `yaml:"username"`
	Password string `yaml:"password"`
}

// ---------------------------------------------------------------------------
// Duration：支持 "30s" / "5m" / "1h" / "7d" / "1w" 等写法
// ---------------------------------------------------------------------------

// Duration 包装 time.Duration，额外支持 d（天）与 w（周）后缀。
type Duration time.Duration

// Std 转换回标准 time.Duration。
func (d Duration) Std() time.Duration { return time.Duration(d) }

func (d Duration) String() string { return time.Duration(d).String() }

// UnmarshalYAML 实现 yaml.Unmarshaler。
func (d *Duration) UnmarshalYAML(value *yaml.Node) error {
	parsed, err := ParseDuration(value.Value)
	if err != nil {
		return err
	}
	*d = parsed
	return nil
}

// MarshalYAML 以字符串形式回写，便于 Web UI 保存。
func (d Duration) MarshalYAML() (any, error) { return d.String(), nil }

// ParseDuration 解析带 d/w 后缀的时长字符串。
func ParseDuration(s string) (Duration, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("时长为空")
	}
	// 纯数字按秒处理，方便环境变量书写。
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		return Duration(time.Duration(n) * time.Second), nil
	}

	lower := strings.ToLower(s)
	// 归一化 "1day"/"1days" 等长写法。
	replacer := strings.NewReplacer(
		"days", "d", "day", "d", "d ", "d",
		"weeks", "w", "week", "w",
		"hours", "h", "hour", "h",
		"minutes", "m", "minute", "m", "mins", "m", "min", "m",
		"seconds", "s", "second", "s", "secs", "s", "sec", "s",
	)
	lower = replacer.Replace(lower)
	lower = strings.Join(strings.Fields(lower), "")

	// 处理 d / w 后缀：拆成小时再交给 time.ParseDuration。
	if strings.HasSuffix(lower, "d") || strings.HasSuffix(lower, "w") {
		unit := lower[len(lower)-1]
		numPart := lower[:len(lower)-1]
		n, err := strconv.ParseFloat(numPart, 64)
		if err != nil {
			return 0, fmt.Errorf("无法解析时长 %q", s)
		}
		mult := 24.0
		if unit == 'w' {
			mult = 24 * 7
		}
		return Duration(time.Duration(n * mult * float64(time.Hour))), nil
	}

	dur, err := time.ParseDuration(lower)
	if err != nil {
		return 0, fmt.Errorf("无法解析时长 %q: %w", s, err)
	}
	return Duration(dur), nil
}

// ---------------------------------------------------------------------------
// 默认值
// ---------------------------------------------------------------------------

// Default 返回一份带合理默认值的配置，用于首次启动生成 config.yaml。
//
// 设计取舍：默认配置应当是【无害的】。
//   - 下载器：给出一份「示例」客户端，但 enabled 为 false。
//     若默认启用并指向一个虚构地址（如 192.168.1.10），
//     用户第一次打开面板只会看到两个红色的错误，不知从何下手。
//   - 策略：默认【不启用】。这是更重要的取舍 ——
//     策略一旦启用且用户没注意到，就会在流量达标后
//     静默把上传限速到 1 MiB/s，这类「悄悄限速」最难排查。
//     用户明确配置好并使用「测试限速」验证过之后再启用。
func Default() *Config {
	yes := true
	no := false
	return &Config{
		Listen:        ":8080",
		DataDir:       "/data",
		PollInterval:  Duration(5 * time.Second),
		LogLevel:      "info",
		Timezone:      "",
		HistoryPoints: 720,
		Clients: []ClientConfig{
			{
				Name:     "qbittorrent",
				Type:     "qbittorrent",
				URL:      "http://192.168.1.10:8080",
				Username: "admin",
				Password: "adminadmin",
				Enabled:  &no, // 示例条目，需用户在面板里填好地址后手动启用
				Timeout:  Duration(10 * time.Second),
			},
			{
				Name:     "transmission",
				Type:     "transmission",
				URL:      "http://192.168.1.10:9091",
				Username: "",
				Password: "",
				Enabled:  &no, // 同上，示例条目默认不启用
				Timeout:  Duration(10 * time.Second),
			},
		},
		Policies: []PolicyConfig{
			{
				Name:                "daily-500g",
				Enabled:             &no, // 需用户确认阈值后再启用，避免「悄悄限速」
				Clients:             []string{"*"},
				Window:              Duration(24 * time.Hour),
				Align:               "day",
				ThresholdTotal:      500 * 1024 * 1024 * 1024, // 500 GiB
				LimitTotal:          1024 * 1024,              // 1 MiB/s
				ResumeOnWindowReset: &yes,
				Cooldown:            Duration(10 * time.Minute),
			},
		},
		Notify: NotifyConfig{
			Enabled:  false,
			NotifyOn: []string{"speedup", "resume", "error"},
			Ntfy: NtfyConfig{
				Enabled:  false,
				URL:      "https://ntfy.sh",
				Topic:    "bt-traffic-guard",
				Priority: "default",
			},
			Webhook: WebhookConfig{
				Enabled: false,
				Method:  "POST",
				Format:  "generic",
			},
		},
		WebAuth: WebAuthConfig{
			Enabled:  false,
			Username: "admin",
			Password: "btguard",
		},
	}
}

// ---------------------------------------------------------------------------
// 加载
// ---------------------------------------------------------------------------

// Load 读取配置文件；文件不存在时返回默认配置与 created=true。
func Load(path string) (cfg *Config, created bool, err error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return Default(), true, nil
		}
		return nil, false, fmt.Errorf("读取配置 %s 失败: %w", path, err)
	}

	// 去掉 UTF-8 BOM。Windows 记事本等编辑器保存时会写入 BOM，
	// 而 yaml 解析器会把 BOM 当作内容的一部分，导致第一个键解析异常
	// （典型症状是 clients[0].type 变成空字符串）。
	raw = bytes.TrimPrefix(raw, []byte{0xEF, 0xBB, 0xBF})

	// 旧版本遗留的 IPv4/IPv6 分族字段：读取时忽略，并尽量就地清理。
	// 详见 migrateLegacyFamilyFields 的说明。
	raw = migrateLegacyFamilyFields(path, raw)

	cfg = Default() // 先铺默认值，yaml 覆盖其上
	if err := yaml.Unmarshal(raw, cfg); err != nil {
		return nil, false, fmt.Errorf("解析配置 %s 失败: %w", path, err)
	}

	applyEnv(cfg)
	if err := cfg.Validate(); err != nil {
		return nil, false, err
	}
	return cfg, false, nil
}

// legacyFamilyKeys 是 v1.1 起不再支持的 IPv4/IPv6 分族字段。
//
// 移除原因：两个下载器的公开 API 都无法提供可靠的分族数据 ——
// Transmission 只能按当前 peer 占比估算、qBittorrent 的 peer 明细
// 在保种空闲时经常缺失。强行估算会产生数量级错误
// （实测出现过 2 TB 级的虚假累计），因此改为只统计总上传量。
var legacyFamilyKeys = []string{
	"threshold_ipv4", "threshold_ipv6",
	"limit_ipv4", "limit_ipv6",
	"resume_limit_ipv4", "resume_limit_ipv6",
	"upload_limit_ipv4", "upload_limit_ipv6",
}

// migrateLegacyFamilyFields 从配置原文中剔除已废弃的分族字段。
//
// 处理策略：
//   - 解析层面：这些键已不在结构体中，yaml 会直接忽略，不影响启动；
//   - 文件层面：若检测到残留，先备份原文件为 <name>.bak，
//     再原地改写，避免用户下次编辑 YAML 时被这些无效字段误导。
//
// 返回可供解析的内容（始终安全）。写盘失败不影响启动。
func migrateLegacyFamilyFields(path string, raw []byte) []byte {
	text := string(raw)
	found := false
	for _, k := range legacyFamilyKeys {
		// 形如 "  threshold_ipv4: 123" / "  limit_ipv4: 0"
		if strings.Contains(text, k+":") {
			found = true
			break
		}
	}
	if !found {
		return raw
	}

	// 逐行删除这些键（保留其它内容与缩进不变）。
	lines := strings.Split(text, "\n")
	kept := make([]string, 0, len(lines))
	for _, ln := range lines {
		trimmed := strings.TrimSpace(ln)
		drop := false
		for _, k := range legacyFamilyKeys {
			if strings.HasPrefix(trimmed, k+":") {
				drop = true
				break
			}
		}
		if !drop {
			kept = append(kept, ln)
		}
	}
	cleaned := []byte(strings.Join(kept, "\n"))

	// 先备份再改写。备份失败则放弃改写，宁可留着冗余字段也不冒险。
	if path != "" {
		bak := path + ".bak"
		if _, err := os.Stat(bak); os.IsNotExist(err) {
			if err := os.WriteFile(bak, raw, 0o600); err == nil {
				_ = os.WriteFile(path, cleaned, 0o600)
			}
		} else {
			// 已有备份（说明之前迁移过），直接改写即可。
			_ = os.WriteFile(path, cleaned, 0o600)
		}
	}
	return cleaned
}

// applyEnv 用 BTG_ 前缀的环境变量覆盖配置。
func applyEnv(cfg *Config) {
	if v := os.Getenv("BTG_LISTEN"); v != "" {
		cfg.Listen = v
	}
	if v := os.Getenv("BTG_DATA_DIR"); v != "" {
		cfg.DataDir = v
	}
	if v := os.Getenv("BTG_LOG_LEVEL"); v != "" {
		cfg.LogLevel = v
	}
	if v := os.Getenv("BTG_TIMEZONE"); v != "" {
		cfg.Timezone = v
	}
	if v := os.Getenv("BTG_POLL_INTERVAL"); v != "" {
		if d, err := ParseDuration(v); err == nil {
			cfg.PollInterval = d
		}
	}
	if v := os.Getenv("BTG_WEB_PASSWORD"); v != "" {
		cfg.WebAuth.Enabled = true
		cfg.WebAuth.Password = v
		if u := os.Getenv("BTG_WEB_USERNAME"); u != "" {
			cfg.WebAuth.Username = u
		}
	}
	// 单客户端快速覆盖，方便纯环境变量部署。
	overlayEnvClient(cfg, "QBITTORRENT", "qbittorrent")
	overlayEnvClient(cfg, "TRANSMISSION", "transmission")
}

func overlayEnvClient(cfg *Config, prefix, clientType string) {
	url := os.Getenv("BTG_" + prefix + "_URL")
	user := os.Getenv("BTG_" + prefix + "_USERNAME")
	pass := os.Getenv("BTG_" + prefix + "_PASSWORD")
	apiKey := os.Getenv("BTG_" + prefix + "_API_KEY")
	if url == "" && user == "" && pass == "" && apiKey == "" {
		return
	}
	for i := range cfg.Clients {
		if cfg.Clients[i].Type != clientType {
			continue
		}
		if url != "" {
			cfg.Clients[i].URL = url
		}
		if user != "" {
			cfg.Clients[i].Username = user
		}
		if pass != "" {
			cfg.Clients[i].Password = pass
		}
		if apiKey != "" {
			cfg.Clients[i].APIKey = apiKey
		}
		return
	}
}

// Validate 校验配置并补齐缺省项。
func (c *Config) Validate() error {
	if c.Listen == "" {
		c.Listen = ":8080"
	}
	if c.DataDir == "" {
		c.DataDir = "/data"
	}
	if c.PollInterval.Std() < time.Second {
		c.PollInterval = Duration(time.Second)
	}
	if c.PollInterval.Std() > 5*time.Minute {
		c.PollInterval = Duration(5 * time.Minute)
	}
	if c.HistoryPoints <= 0 {
		c.HistoryPoints = 720
	}
	switch strings.ToLower(c.LogLevel) {
	case "debug", "info", "warn", "error":
		c.LogLevel = strings.ToLower(c.LogLevel)
	default:
		c.LogLevel = "info"
	}

	seen := map[string]bool{}
	for i := range c.Clients {
		cl := &c.Clients[i]
		cl.Name = strings.TrimSpace(cl.Name)
		if cl.Name == "" {
			return fmt.Errorf("clients[%d]: name 不能为空", i)
		}
		key := strings.ToLower(cl.Name)
		if seen[key] {
			return fmt.Errorf("clients[%d]: name %q 重复", i, cl.Name)
		}
		seen[key] = true

		cl.Type = strings.ToLower(strings.TrimSpace(cl.Type))
		switch cl.Type {
		case "qbittorrent", "transmission":
		default:
			return fmt.Errorf("clients[%d] (%s): type 必须是 qbittorrent 或 transmission，当前为 %q", i, cl.Name, cl.Type)
		}
		cl.URL = strings.TrimRight(strings.TrimSpace(cl.URL), "/")
		if cl.URL == "" {
			return fmt.Errorf("clients[%d] (%s): url 不能为空", i, cl.Name)
		}
		if !strings.HasPrefix(cl.URL, "http://") && !strings.HasPrefix(cl.URL, "https://") {
			cl.URL = "http://" + cl.URL
		}
		if cl.Timeout.Std() <= 0 {
			cl.Timeout = Duration(10 * time.Second)
		}

		// API Key 校验。只做格式提示，不阻断启动——因为 Key 的格式
		// 可能随 qBittorrent 版本变化，过度严格会导致升级后无法启动。
		cl.APIKey = strings.TrimSpace(cl.APIKey)
		if cl.APIKey != "" {
			if cl.Type != "qbittorrent" {
				// Transmission 没有这个机制，清掉以免造成误解。
				cl.APIKey = ""
			} else if !strings.HasPrefix(cl.APIKey, "qbt_") {
				// 官方格式为 qbt_ 前缀 + 28 位。这里仅提示，
				// 因为用户可能从别处粘贴时带了引号或空格。
				cl.APIKey = ""
				return fmt.Errorf("clients[%d] (%s): api_key 格式不正确，应以 qbt_ 开头（qBittorrent 5.2.0 及以上版本在「设置 → WebUI → API Key」中生成）", i, cl.Name)
			}
		}
	}

	if len(c.Policies) == 0 {
		return fmt.Errorf("policies 不能为空，至少需要一条限速策略")
	}
	pseen := map[string]bool{}
	for i := range c.Policies {
		p := &c.Policies[i]
		p.Name = strings.TrimSpace(p.Name)
		if p.Name == "" {
			p.Name = fmt.Sprintf("policy-%d", i+1)
		}
		key := strings.ToLower(p.Name)
		if pseen[key] {
			return fmt.Errorf("policies[%d]: name %q 重复", i, p.Name)
		}
		pseen[key] = true

		if p.Window.Std() <= 0 {
			p.Window = Duration(24 * time.Hour)
		}
		p.Align = strings.ToLower(strings.TrimSpace(p.Align))
		switch p.Align {
		case "", "rolling":
			p.Align = "rolling"
		case "hour", "day", "week", "month":
		default:
			return fmt.Errorf("policies[%d] (%s): align 取值非法 %q（可选 rolling/hour/day/week/month）", i, p.Name, p.Align)
		}

		if p.ThresholdTotal <= 0 {
			return fmt.Errorf("policies[%d] (%s): 必须设置 threshold_total（本周期允许的上传总量）", i, p.Name)
		}
		if p.LimitTotal < 0 {
			return fmt.Errorf("policies[%d] (%s): 限速值不能为负", i, p.Name)
		}
		// 校验 clients 名单中的名字都存在。
		for _, n := range p.Clients {
			if n == "*" {
				continue
			}
			if !seen[strings.ToLower(n)] {
				return fmt.Errorf("policies[%d] (%s): clients 中的 %q 未在 clients 段定义", i, p.Name, n)
			}
		}
	}

	if c.Notify.Ntfy.Enabled {
		if c.Notify.Ntfy.URL == "" {
			c.Notify.Ntfy.URL = "https://ntfy.sh"
		}
		if strings.TrimSpace(c.Notify.Ntfy.Topic) == "" {
			return fmt.Errorf("notify.ntfy: 启用后 topic 不能为空")
		}
	}
	if c.Notify.Webhook.Enabled && strings.TrimSpace(c.Notify.Webhook.URL) == "" {
		return fmt.Errorf("notify.webhook: 启用后 url 不能为空")
	}
	if c.Notify.Webhook.Method == "" {
		c.Notify.Webhook.Method = "POST"
	}
	if c.Notify.Webhook.Format == "" {
		c.Notify.Webhook.Format = "generic"
	}
	if c.WebAuth.Enabled && c.WebAuth.Password == "" {
		return fmt.Errorf("web_auth: 启用后 password 不能为空")
	}

	if c.Timezone != "" {
		if _, err := time.LoadLocation(c.Timezone); err != nil {
			return fmt.Errorf("timezone %q 无效: %w", c.Timezone, err)
		}
	}
	return nil
}

// Location 返回配置的时区，未设置时返回本地时区。
func (c *Config) Location() *time.Location {
	if c.Timezone == "" {
		return time.Local
	}
	loc, err := time.LoadLocation(c.Timezone)
	if err != nil {
		return time.Local
	}
	return loc
}

// ---------------------------------------------------------------------------
// 保存
// ---------------------------------------------------------------------------

// Save 把配置写回 yaml 文件（原子替换）。
func Save(path string, cfg *Config) error {
	out, err := yaml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("序列化配置失败: %w", err)
	}
	header := "# Traffic Guard 配置文件\n" +
		"# 修改后可在 Web UI 点击「重载配置」，或等待自动热重载（每 3 秒检查一次）。\n" +
		"# 上传限速单位：字节/秒，0 表示不限速。阈值单位：字节。\n\n"
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append([]byte(header), out...), 0o644); err != nil {
		return fmt.Errorf("写入临时配置失败: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("替换配置文件失败: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// 周期窗口
// ---------------------------------------------------------------------------

// WindowBounds 返回给定时刻所属统计周期的起止时间。
//
// align 语义：
//   - rolling：以 start 为锚点、长度 window 的滚动周期
//   - hour/day/week/month：自然周期，长度由 window 向上取整到该自然单位决定
func WindowBounds(align string, window time.Duration, now time.Time, rollingStart time.Time, loc *time.Location) (time.Time, time.Time) {
	now = now.In(loc)
	switch align {
	case "hour":
		start := time.Date(now.Year(), now.Month(), now.Day(), now.Hour(), 0, 0, 0, loc)
		n := int(window.Hours())
		if n < 1 {
			n = 1
		}
		// 对齐到 window 小时块
		start = start.Add(-time.Duration(start.Hour()%n) * time.Hour)
		return start, start.Add(time.Duration(n) * time.Hour)

	case "day":
		start := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, loc)
		n := int(window.Hours() / 24)
		if n < 1 {
			n = 1
		}
		return start, start.AddDate(0, 0, n)

	case "week":
		// 以周一为一周起点。
		start := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, loc)
		offset := (int(start.Weekday()) + 6) % 7
		start = start.AddDate(0, 0, -offset)
		n := int(window.Hours() / (24 * 7))
		if n < 1 {
			n = 1
		}
		return start, start.AddDate(0, 0, 7*n)

	case "month":
		start := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, loc)
		n := int(window.Hours() / (24 * 30))
		if n < 1 {
			n = 1
		}
		return start, start.AddDate(0, n, 0)

	default: // rolling
		if rollingStart.IsZero() {
			rollingStart = now
		}
		if window <= 0 {
			window = 24 * time.Hour
		}
		elapsed := now.Sub(rollingStart)
		if elapsed < 0 {
			return rollingStart, rollingStart.Add(window)
		}
		periods := elapsed / window
		start := rollingStart.Add(periods * window)
		return start, start.Add(window)
	}
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

// ParseBytes 解析 "500G" / "1.5TB" / "1024M" 之类的容量字符串。
func ParseBytes(s string) (int64, error) {
	s = strings.TrimSpace(strings.ToUpper(s))
	if s == "" {
		return 0, fmt.Errorf("容量为空")
	}
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		return n, nil
	}
	units := []struct {
		suffix string
		mult   float64
	}{
		{"TIB", 1 << 40}, {"TB", 1e12}, {"TI", 1 << 40}, {"T", 1e12},
		{"GIB", 1 << 30}, {"GB", 1e9}, {"GI", 1 << 30}, {"G", 1e9},
		{"MIB", 1 << 20}, {"MB", 1e6}, {"MI", 1 << 20}, {"M", 1e6},
		{"KIB", 1 << 10}, {"KB", 1e3}, {"KI", 1 << 10}, {"K", 1e3},
		{"B", 1},
	}
	for _, u := range units {
		if strings.HasSuffix(s, u.suffix) {
			num := strings.TrimSpace(strings.TrimSuffix(s, u.suffix))
			f, err := strconv.ParseFloat(num, 64)
			if err != nil {
				return 0, fmt.Errorf("无法解析容量 %q", s)
			}
			return int64(f * u.mult), nil
		}
	}
	return 0, fmt.Errorf("无法解析容量 %q", s)
}

// ParseBitrate 解析 "1M" / "512K" / "2Mbps" 之类的速率字符串为字节/秒。
func ParseBitrate(s string) (int64, error) {
	s = strings.TrimSpace(strings.ToUpper(s))
	s = strings.TrimSuffix(s, "PS")
	s = strings.TrimSuffix(s, "/S")
	if s == "" {
		return 0, fmt.Errorf("速率为空")
	}
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		return n, nil
	}
	units := []struct {
		suffix string
		mult   float64
	}{
		{"TIB", 1 << 40}, {"TB", 1e12}, {"T", 1e12},
		{"GIB", 1 << 30}, {"GB", 1e9}, {"G", 1e9},
		{"MIB", 1 << 20}, {"MB", 1e6}, {"M", 1e6},
		{"KIB", 1 << 10}, {"KB", 1e3}, {"K", 1e3},
		{"B", 1},
	}
	for _, u := range units {
		if strings.HasSuffix(s, u.suffix) {
			num := strings.TrimSpace(strings.TrimSuffix(s, u.suffix))
			f, err := strconv.ParseFloat(num, 64)
			if err != nil {
				return 0, fmt.Errorf("无法解析速率 %q", s)
			}
			return int64(f * u.mult), nil
		}
	}
	return 0, fmt.Errorf("无法解析速率 %q", s)
}

// NormalizeURL 确保地址带有 scheme 且无尾斜杠。
func NormalizeURL(raw string) string {
	raw = strings.TrimRight(strings.TrimSpace(raw), "/")
	if raw == "" {
		return raw
	}
	if !strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://") {
		raw = "http://" + raw
	}
	return raw
}

// IsLoopback 判断地址是否指向本机（用于给出配置建议）。
// IsLoopback 判断地址是否指向本机（用于在容器场景下给出配置建议）。
//
// 必须能正确处理 IPv6：IPv6 地址本身含冒号，
// 若用「按第一个冒号切分」的朴素做法，会把 [::1]:8080 切成 "["
// 而无法识别，导致本该给出的提示漏掉。
func IsLoopback(rawURL string) bool {
	host := strings.TrimSpace(rawURL)
	if host == "" {
		return false
	}

	// 优先用 url.Parse，它能正确处理 [::1]:8080 这类带方括号的写法。
	if strings.Contains(host, "://") {
		if u, err := url.Parse(host); err == nil && u.Host != "" {
			host = u.Host
		}
	}

	// 去掉可能存在的端口。
	// 注意：只有在「非 IPv6 字面量」或「已去掉方括号」时才安全地按冒号切分。
	if strings.HasPrefix(host, "[") {
		// 形如 [::1]:8080 或 [::1]
		if i := strings.Index(host, "]"); i > 0 {
			host = host[1:i]
		}
	} else if ip := net.ParseIP(host); ip == nil {
		// 不是裸 IP，说明可能带端口（如 127.0.0.1:8080 或 nas.local:8080）
		if i := strings.LastIndex(host, ":"); i > 0 {
			host = host[:i]
		}
	}

	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
