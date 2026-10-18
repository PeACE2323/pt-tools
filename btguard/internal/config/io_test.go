package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// ---------------------------------------------------------------------------
// Save：配置落盘
// ---------------------------------------------------------------------------

// TestSaveRoundTrip 验证保存后再读取能还原等价配置。
//
// Save 此前完全没有测试覆盖，而它是 WebUI 保存配置的唯一落盘路径 ——
// 写坏了意味着用户改了配置、重启后又变回去。
func TestSaveRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")

	yes := true
	orig := Default()
	orig.Listen = ":7777"
	orig.PollInterval = Duration(3 * time.Second)
	orig.LogLevel = "debug"
	orig.Timezone = "Asia/Tokyo"
	orig.Clients = []ClientConfig{{
		Name: "qb", Type: "qbittorrent", URL: "http://192.168.1.5:8080",
		Username: "admin", Password: "pw", APIKey: "qbt_roundtrip1234567890abcdef",
		Enabled: &yes, Timeout: Duration(7 * time.Second),
	}}
	orig.Policies = []PolicyConfig{{
		Name: "p", Enabled: &yes, Clients: []string{"qb"},
		Window: Duration(24 * time.Hour), Align: "day",
		ThresholdTotal: Bytes(500 * (1 << 30)), LimitTotal: Bitrate(1 << 20),
		ResumeOnWindowReset: &yes, Cooldown: Duration(5 * time.Minute),
	}}

	if err := Save(path, orig); err != nil {
		t.Fatalf("Save 失败: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("配置文件未生成: %v", err)
	}

	got, created, err := Load(path)
	if err != nil {
		t.Fatalf("Load 失败: %v", err)
	}
	if created {
		t.Error("文件已存在，created 应为 false")
	}

	// 逐项核对关键字段
	if got.Listen != ":7777" {
		t.Errorf("listen = %q", got.Listen)
	}
	if got.PollInterval.Std() != 3*time.Second {
		t.Errorf("poll_interval = %v", got.PollInterval.Std())
	}
	if got.LogLevel != "debug" {
		t.Errorf("log_level = %q", got.LogLevel)
	}
	if got.Timezone != "Asia/Tokyo" {
		t.Errorf("timezone = %q", got.Timezone)
	}
	if len(got.Clients) != 1 {
		t.Fatalf("客户端数 = %d", len(got.Clients))
	}
	c := got.Clients[0]
	if c.URL != "http://192.168.1.5:8080" {
		t.Errorf("url = %q", c.URL)
	}
	if c.Password != "pw" {
		t.Errorf("密码未还原: %q", c.Password)
	}
	if c.APIKey != "qbt_roundtrip1234567890abcdef" {
		t.Errorf("API Key 未还原: %q", c.APIKey)
	}
	if c.Timeout.Std() != 7*time.Second {
		t.Errorf("timeout = %v", c.Timeout.Std())
	}
	if len(got.Policies) != 1 {
		t.Fatalf("策略数 = %d", len(got.Policies))
	}
	p := got.Policies[0]
	if p.ThTotal() != 500*(1<<30) {
		t.Errorf("阈值未还原: %d", p.ThTotal())
	}
	if p.LimTotal() != 1<<20 {
		t.Errorf("限速未还原: %d", p.LimTotal())
	}
	if p.Cooldown.Std() != 5*time.Minute {
		t.Errorf("冷却期未还原: %v", p.Cooldown.Std())
	}
	if !p.ShouldResume() {
		t.Error("resume_on_window_reset 未还原")
	}
}

// TestSaveIsAtomic 验证保存使用「写临时文件 + 重命名」的原子替换。
//
// 若直接覆盖写，进程在写入中途被杀会留下半截文件，
// 下次启动会因解析失败而无法启动。
func TestSaveIsAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")

	if err := Save(path, Default()); err != nil {
		t.Fatal(err)
	}
	// 保存后不应残留 .tmp 文件
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Errorf("保存后残留临时文件: %s", e.Name())
		}
	}
}

// TestSaveOverwritesExisting 验证可以被反复保存（WebUI 多次点保存的场景）。
func TestSaveOverwritesExisting(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")

	for i, listen := range []string{":1111", ":2222", ":3333"} {
		cfg := Default()
		cfg.Listen = listen
		if err := Save(path, cfg); err != nil {
			t.Fatalf("第 %d 次保存失败: %v", i+1, err)
		}
		got, _, err := Load(path)
		if err != nil {
			t.Fatalf("第 %d 次读取失败: %v", i+1, err)
		}
		if got.Listen != listen {
			t.Errorf("第 %d 次: listen = %q, 期望 %q", i+1, got.Listen, listen)
		}
	}
}

// TestSaveWritesHeaderComment 验证生成的文件带有说明性注释，
// 便于用户手工编辑时理解字段含义。
func TestSaveWritesHeaderComment(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := Save(path, Default()); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	text := string(raw)
	if !strings.HasPrefix(text, "#") {
		t.Error("文件应以注释开头")
	}
	if !strings.Contains(text, "字节/秒") {
		t.Error("文件头应说明限速单位")
	}
}

// TestSaveFailsOnUnwritableDir 验证目录不可写时返回错误而不是静默失败。
func TestSaveFailsOnUnwritableDir(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("以 root 运行时权限位不生效")
	}
	dir := t.TempDir()
	ro := filepath.Join(dir, "ro")
	if err := os.MkdirAll(ro, 0o555); err != nil {
		t.Fatal(err)
	}
	err := Save(filepath.Join(ro, "sub", "config.yaml"), Default())
	if err == nil {
		t.Skip("当前环境未强制权限位，跳过")
	}
}

// ---------------------------------------------------------------------------
// Location：时区（决定周期何时重置）
// ---------------------------------------------------------------------------

// TestLocationResolvesTimezone 验证时区解析。
//
// timezone 决定「自然日」从几点开始算：
// 配错会导致流量在错误的时间点归零，用户会以为计数丢失。
func TestLocationResolvesTimezone(t *testing.T) {
	cfg := Default()

	// 未设置 -> 本地时区
	cfg.Timezone = ""
	if loc := cfg.Location(); loc == nil {
		t.Error("未设置时区时应返回本地时区，而不是 nil")
	}

	// 合法时区
	cfg.Timezone = "Asia/Shanghai"
	loc := cfg.Location()
	if loc == nil {
		t.Fatal("合法时区不应返回 nil")
	}
	if loc.String() != "Asia/Shanghai" {
		t.Errorf("时区 = %q, 期望 Asia/Shanghai", loc.String())
	}

	cfg.Timezone = "UTC"
	if got := cfg.Location().String(); got != "UTC" {
		t.Errorf("时区 = %q, 期望 UTC", got)
	}
}

// TestLocationFallsBackOnInvalid 验证非法时区回退到本地时区而不 panic。
//
// Validate 会拒绝非法时区，但 Location 仍应具备防御性 ——
// 它可能在配置未经过 Validate 的路径上被调用。
func TestLocationFallsBackOnInvalid(t *testing.T) {
	cfg := Default()
	cfg.Timezone = "Not/A/Real_Zone"
	loc := cfg.Location()
	if loc == nil {
		t.Fatal("非法时区应回退到本地时区，而不是返回 nil")
	}
}

// TestTimezoneAffectsWindowBounds 验证时区确实影响周期边界。
//
// 同一个 UTC 时刻，在东八区与 UTC 下所属的「自然日」不同。
func TestTimezoneAffectsWindowBounds(t *testing.T) {
	// 2026-03-15 16:30 UTC = 2026-03-16 00:30 东八区
	instant := time.Date(2026, 3, 15, 16, 30, 0, 0, time.UTC)

	shanghai, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		t.Skip("时区数据库不可用")
	}

	s1, _ := WindowBounds("day", 24*time.Hour, instant, time.Time{}, shanghai)
	s2, _ := WindowBounds("day", 24*time.Hour, instant, time.Time{}, time.UTC)

	// 东八区已跨到 3-16，UTC 仍在 3-15
	if s1.Day() != 16 {
		t.Errorf("东八区的日期 = %d, 期望 16", s1.Day())
	}
	if s2.Day() != 15 {
		t.Errorf("UTC 的日期 = %d, 期望 15", s2.Day())
	}
	if s1.Equal(s2) {
		t.Error("不同时区下的周期起点不应相同")
	}
}

// ---------------------------------------------------------------------------
// YAML 序列化的辅助方法
// ---------------------------------------------------------------------------

// TestBytesAndBitrateMarshal 验证 Bytes / Bitrate 回写为整数。
//
// 回写格式很重要：若序列化成 "500GiB" 这样的字符串，
// 再次读取时虽然能解析，但与用户手写的配置混在一起会难以比对；
// 序列化成整数则始终可被安全读回。
func TestBytesAndBitrateMarshal(t *testing.T) {
	type wrapper struct {
		B Bytes   `yaml:"b"`
		R Bitrate `yaml:"r"`
	}
	in := wrapper{B: Bytes(500 * (1 << 30)), R: Bitrate(1 << 20)}

	out, err := yaml.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	text := string(out)

	// 应输出为纯整数
	if !strings.Contains(text, "536870912000") {
		t.Errorf("Bytes 应序列化为整数，实际:\n%s", text)
	}
	if !strings.Contains(text, "1048576") {
		t.Errorf("Bitrate 应序列化为整数，实际:\n%s", text)
	}

	// 必须能被读回
	var back wrapper
	if err := yaml.Unmarshal(out, &back); err != nil {
		t.Fatalf("读回失败: %v", err)
	}
	if back.B != in.B || back.R != in.R {
		t.Errorf("往返不一致: %+v -> %+v", in, back)
	}
}

// TestDurationMarshalRoundTrip 验证 Duration 的字符串往返。
func TestDurationMarshalRoundTrip(t *testing.T) {
	type wrapper struct {
		D Duration `yaml:"d"`
	}
	cases := []time.Duration{
		30 * time.Second,
		5 * time.Minute,
		24 * time.Hour,
		7 * 24 * time.Hour,
	}
	for _, d := range cases {
		out, err := yaml.Marshal(wrapper{D: Duration(d)})
		if err != nil {
			t.Fatalf("%v 序列化失败: %v", d, err)
		}
		var back wrapper
		if err := yaml.Unmarshal(out, &back); err != nil {
			t.Fatalf("%v 读回失败: %v", d, err)
		}
		if back.D.Std() != d {
			t.Errorf("往返不一致: %v -> %v (yaml: %s)", d, back.D.Std(), strings.TrimSpace(string(out)))
		}
	}
}

// TestInt64AndStringAccessors 验证 Bytes/Bitrate 的取值方法。
func TestInt64AndStringAccessors(t *testing.T) {
	b := Bytes(12345)
	if b.Int64() != 12345 {
		t.Errorf("Bytes.Int64() = %d", b.Int64())
	}
	if b.String() != "12345" {
		t.Errorf("Bytes.String() = %q", b.String())
	}

	r := Bitrate(67890)
	if r.Int64() != 67890 {
		t.Errorf("Bitrate.Int64() = %d", r.Int64())
	}
	if r.String() != "67890" {
		t.Errorf("Bitrate.String() = %q", r.String())
	}
}

// ---------------------------------------------------------------------------
// 策略配置的访问器
// ---------------------------------------------------------------------------

// TestPolicyAccessors 验证策略各访问器返回正确的值。
//
// 这些访问器是策略引擎读取阈值与限速的唯一入口，
// 返回错误的值会直接导致限速算错。
func TestPolicyAccessors(t *testing.T) {
	p := PolicyConfig{
		ThresholdTotal: Bytes(1000),
		LimitTotal:     Bitrate(100),
	}

	if p.ThTotal() != 1000 {
		t.Errorf("ThTotal() = %d, 期望 1000", p.ThTotal())
	}
	if p.LimTotal() != 100 {
		t.Errorf("LimTotal() = %d, 期望 100", p.LimTotal())
	}
	if !p.HasThreshold() {
		t.Error("配置了阈值时 HasThreshold 应为 true")
	}

	// 全零的边界情况
	zero := PolicyConfig{}
	if zero.HasThreshold() {
		t.Error("未配置阈值时 HasThreshold 应为 false")
	}
}

// TestPolicyShouldResumeDefault 验证未显式设置时默认自动解除限速。
func TestPolicyShouldResumeDefault(t *testing.T) {
	p := PolicyConfig{}
	if !p.ShouldResume() {
		t.Error("未设置 resume_on_window_reset 时应默认为 true")
	}

	no := false
	p.ResumeOnWindowReset = &no
	if p.ShouldResume() {
		t.Error("显式设为 false 时应返回 false")
	}
}

// TestClientConfigAuthMode 验证认证方式的判定与描述。
func TestClientConfigAuthMode(t *testing.T) {
	cases := []struct {
		name string
		cfg  ClientConfig
		want string
	}{
		{"qb + API Key", ClientConfig{Type: "qbittorrent", APIKey: "qbt_x"}, "API Key"},
		{"qb + 账号密码", ClientConfig{Type: "qbittorrent", Username: "a", Password: "b"}, "用户名密码"},
		{"qb 无凭据", ClientConfig{Type: "qbittorrent"}, "无认证"},
		// API Key 只对 qBittorrent 有效；Transmission 即便误填也应走账号密码
		{"tr + API Key（应忽略）", ClientConfig{Type: "transmission", APIKey: "qbt_x"}, "无认证"},
		{"tr + 账号密码", ClientConfig{Type: "transmission", Username: "a", Password: "b"}, "用户名密码"},
	}
	for _, tc := range cases {
		if got := tc.cfg.AuthMode(); got != tc.want {
			t.Errorf("%s: AuthMode() = %q, 期望 %q", tc.name, got, tc.want)
		}
	}

	if !(ClientConfig{Type: "qbittorrent", APIKey: "qbt_x"}).UsesAPIKey() {
		t.Error("qBittorrent 配置了 Key 时 UsesAPIKey 应为 true")
	}
	if (ClientConfig{Type: "transmission", APIKey: "qbt_x"}).UsesAPIKey() {
		t.Error("Transmission 不应使用 API Key")
	}
	if (ClientConfig{Type: "qbittorrent", APIKey: "  "}).UsesAPIKey() {
		t.Error("仅空白的 API Key 应视为未配置")
	}
}

// TestShouldNotify 验证通知事件的过滤逻辑。
func TestShouldNotify(t *testing.T) {
	// 总开关关闭时一律不通知
	off := NotifyConfig{Enabled: false, NotifyOn: []string{"speedup"}}
	if off.ShouldNotify("speedup") {
		t.Error("未启用时不应通知")
	}

	// 空白名单表示全部通知
	all := NotifyConfig{Enabled: true}
	for _, e := range []string{"speedup", "resume", "error", "anything"} {
		if !all.ShouldNotify(e) {
			t.Errorf("空白名单应通知 %q", e)
		}
	}

	// 白名单过滤，且忽略大小写
	filtered := NotifyConfig{Enabled: true, NotifyOn: []string{"SpeedUp"}}
	if !filtered.ShouldNotify("speedup") {
		t.Error("白名单匹配应忽略大小写")
	}
	if filtered.ShouldNotify("resume") {
		t.Error("不在白名单的事件不应通知")
	}
}

// TestIsLoopback 验证本机地址识别（用于给出配置建议）。
func TestIsLoopback(t *testing.T) {
	loopback := []string{
		"http://127.0.0.1:8080", "http://localhost:9091",
		"http://[::1]:8080", "127.0.0.1",
	}
	for _, u := range loopback {
		if !IsLoopback(u) {
			t.Errorf("%q 应被识别为本机地址", u)
		}
	}
	notLoopback := []string{
		"http://192.168.1.10:8080", "http://10.0.0.5:9091", "http://nas.local:8080",
	}
	for _, u := range notLoopback {
		if IsLoopback(u) {
			t.Errorf("%q 不应被识别为本机地址", u)
		}
	}
}
