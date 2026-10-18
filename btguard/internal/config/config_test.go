package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseDuration(t *testing.T) {
	cases := []struct {
		in   string
		want time.Duration
	}{
		{"30s", 30 * time.Second},
		{"5m", 5 * time.Minute},
		{"24h", 24 * time.Hour},
		{"1d", 24 * time.Hour},
		{"7d", 7 * 24 * time.Hour},
		{"30d", 30 * 24 * time.Hour},
		{"1w", 7 * 24 * time.Hour},
		{"1day", 24 * time.Hour},
		{"2days", 48 * time.Hour},
		{"1hour", time.Hour},
		{"90", 90 * time.Second}, // 纯数字按秒
	}
	for _, c := range cases {
		got, err := ParseDuration(c.in)
		if err != nil {
			t.Errorf("ParseDuration(%q) 返回错误: %v", c.in, err)
			continue
		}
		if got.Std() != c.want {
			t.Errorf("ParseDuration(%q) = %v, 期望 %v", c.in, got.Std(), c.want)
		}
	}

	for _, bad := range []string{"", "abc", "12x"} {
		if _, err := ParseDuration(bad); err == nil {
			t.Errorf("ParseDuration(%q) 应当返回错误", bad)
		}
	}
}

func TestParseBytes(t *testing.T) {
	cases := []struct {
		in   string
		want int64
	}{
		{"1024", 1024},
		{"1KiB", 1024},
		{"1K", 1000},
		{"1MiB", 1 << 20},
		{"1M", 1000000},
		{"500GiB", 500 * (1 << 30)},
		{"1.5TB", 1500000000000},
		{"2TiB", 2 * (1 << 40)},
		{"0", 0},
	}
	for _, c := range cases {
		got, err := ParseBytes(c.in)
		if err != nil {
			t.Errorf("ParseBytes(%q) 返回错误: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("ParseBytes(%q) = %d, 期望 %d", c.in, got, c.want)
		}
	}
}

func TestParseBitrate(t *testing.T) {
	cases := []struct {
		in   string
		want int64
	}{
		{"1048576", 1048576},
		{"1MiB", 1 << 20},
		{"512K", 512000},
		{"2Mbps", 2000000},
		{"1M/s", 1000000},
		{"0", 0},
	}
	for _, c := range cases {
		got, err := ParseBitrate(c.in)
		if err != nil {
			t.Errorf("ParseBitrate(%q) 返回错误: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("ParseBitrate(%q) = %d, 期望 %d", c.in, got, c.want)
		}
	}
}

func TestWindowBounds(t *testing.T) {
	loc := time.FixedZone("CST", 8*3600)
	// 2026-03-15 是周日
	now := time.Date(2026, 3, 15, 14, 30, 0, 0, loc)

	t.Run("day", func(t *testing.T) {
		s, e := WindowBounds("day", 24*time.Hour, now, time.Time{}, loc)
		if s.Format("2006-01-02 15:04") != "2026-03-15 00:00" {
			t.Errorf("day 起点 = %v", s)
		}
		if e.Format("2006-01-02 15:04") != "2026-03-16 00:00" {
			t.Errorf("day 终点 = %v", e)
		}
	})

	t.Run("week 以周一为起点", func(t *testing.T) {
		s, e := WindowBounds("week", 7*24*time.Hour, now, time.Time{}, loc)
		// 2026-03-15 是周日，应归入 03-09（周一）那一周
		if s.Format("2006-01-02") != "2026-03-09" {
			t.Errorf("week 起点 = %v, 期望 2026-03-09", s.Format("2006-01-02"))
		}
		if e.Format("2006-01-02") != "2026-03-16" {
			t.Errorf("week 终点 = %v, 期望 2026-03-16", e.Format("2006-01-02"))
		}
	})

	t.Run("month", func(t *testing.T) {
		s, e := WindowBounds("month", 30*24*time.Hour, now, time.Time{}, loc)
		if s.Format("2006-01-02") != "2026-03-01" {
			t.Errorf("month 起点 = %v", s.Format("2006-01-02"))
		}
		if e.Format("2006-01-02") != "2026-04-01" {
			t.Errorf("month 终点 = %v", e.Format("2006-01-02"))
		}
	})

	t.Run("rolling 必须晚于当前时刻", func(t *testing.T) {
		anchor := time.Date(2026, 3, 15, 10, 0, 0, 0, loc)
		now2 := anchor.Add(150 * time.Minute) // 2.5 个 60 分钟周期
		s, e := WindowBounds("rolling", time.Hour, now2, anchor, loc)
		if s.Format("15:04") != "12:00" {
			t.Errorf("rolling 起点 = %v, 期望 12:00", s.Format("15:04"))
		}
		if e.Format("15:04") != "13:00" {
			t.Errorf("rolling 终点 = %v, 期望 13:00", e.Format("15:04"))
		}
		if !e.After(now2) {
			t.Errorf("rolling 终点 %v 必须晚于当前时刻 %v", e, now2)
		}
	})
}

// TestLoadWithBOM 验证带 UTF-8 BOM 的配置文件能被正确解析。
// Windows 记事本保存 yaml 时会写入 BOM，若不处理会导致首个键解析异常
// （典型症状：clients[0].type 变成空字符串）。
func TestLoadWithBOM(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")

	content := "listen: \":9999\"\n" +
		"clients:\n" +
		"  - name: qb\n" +
		"    type: qbittorrent\n" +
		"    url: \"http://127.0.0.1:8080\"\n" +
		"policies:\n" +
		"  - name: p1\n" +
		"    window: 24h\n" +
		"    align: day\n" +
		"    threshold_total: 500GiB\n" +
		"    limit_total: 1MiB\n"

	// 带 BOM 写入
	if err := os.WriteFile(path, append([]byte{0xEF, 0xBB, 0xBF}, []byte(content)...), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, created, err := Load(path)
	if err != nil {
		t.Fatalf("加载带 BOM 的配置失败: %v", err)
	}
	if created {
		t.Fatal("文件已存在，created 应为 false")
	}
	if cfg.Listen != ":9999" {
		t.Errorf("listen = %q, 期望 \":9999\"", cfg.Listen)
	}
	if len(cfg.Clients) != 1 || cfg.Clients[0].Type != "qbittorrent" {
		t.Fatalf("clients 解析异常: %+v", cfg.Clients)
	}
	if cfg.Policies[0].ThTotal() != 500*(1<<30) {
		t.Errorf("threshold_total = %d, 期望 %d", cfg.Policies[0].ThTotal(), int64(500*(1<<30)))
	}
	if cfg.Policies[0].LimTotal() != 1<<20 {
		t.Errorf("limit_total = %d, 期望 %d", cfg.Policies[0].LimTotal(), int64(1<<20))
	}
}

func TestValidateRejectsBadConfig(t *testing.T) {
	cases := []struct {
		name string
		mut  func(*Config)
	}{
		{"未知客户端类型", func(c *Config) { c.Clients[0].Type = "utorrent" }},
		{"客户端名为空", func(c *Config) { c.Clients[0].Name = "" }},
		{"客户端名重复", func(c *Config) { c.Clients[1].Name = c.Clients[0].Name }},
		{"缺少阈值", func(c *Config) {
			c.Policies[0].ThresholdTotal = 0
		}},
		{"align 非法", func(c *Config) { c.Policies[0].Align = "fortnight" }},
		{"策略引用了不存在的客户端", func(c *Config) { c.Policies[0].Clients = []string{"nope"} }},
		{"ntfy 启用但无 topic", func(c *Config) {
			c.Notify.Ntfy.Enabled = true
			c.Notify.Ntfy.Topic = ""
		}},
		{"web_auth 启用但无密码", func(c *Config) {
			c.WebAuth.Enabled = true
			c.WebAuth.Password = ""
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := Default()
			// 这些用例校验的是「用户配置有误」的场景，
			// 而默认的示例条目是禁用的，需先启用才谈得上被校验。
			if len(cfg.Clients) > 0 {
				en := true
				cfg.Clients[0].Enabled = &en
			}
			tc.mut(cfg)
			if err := cfg.Validate(); err == nil {
				t.Errorf("期望校验失败，但通过了")
			}
		})
	}

	// 默认配置本身必须是合法的
	if err := Default().Validate(); err != nil {
		t.Errorf("默认配置应当合法，却报错: %v", err)
	}
}

// TestDefaultIsHarmless 验证默认配置是「无害」的。
//
// 这是一条产品约束，不是纯粹的代码细节：
//   - 示例下载器若默认启用并指向虚构地址（192.168.1.10），
//     新用户第一次打开面板只会看到一片红色错误，不知从何下手；
//   - 限速策略若默认启用，用户配置好真实下载器后若没注意到它，
//     会在流量达标后被【静默限速到 1 MiB/s】——
//     这类「悄悄改行为」的问题最难排查，必须从默认值上避免。
func TestDefaultIsHarmless(t *testing.T) {
	cfg := Default()

	if len(cfg.Clients) == 0 {
		t.Fatal("默认配置应包含示例下载器，供用户参考字段格式")
	}
	for _, c := range cfg.Clients {
		if c.IsEnabled() {
			t.Errorf("默认下载器 %q 不应启用（会指向虚构地址并在面板上报错）", c.Name)
		}
	}

	if len(cfg.Policies) == 0 {
		t.Fatal("默认配置应包含示例策略")
	}
	for _, p := range cfg.Policies {
		if p.IsEnabled() {
			t.Errorf("默认策略 %q 不应启用（会导致静默限速）", p.Name)
		}
	}

	// 通知与面板认证默认关闭，避免产生用户未预期的对外行为
	if cfg.Notify.Enabled {
		t.Error("默认不应启用通知")
	}
	if cfg.WebAuth.Enabled {
		t.Error("默认不应启用面板认证")
	}
}

func TestPolicyAppliesTo(t *testing.T) {
	p := PolicyConfig{Clients: nil}
	if !p.AppliesTo("anything") {
		t.Error("clients 为空时应匹配全部")
	}

	p = PolicyConfig{Clients: []string{"*"}}
	if !p.AppliesTo("anything") {
		t.Error("通配符应匹配全部")
	}

	p = PolicyConfig{Clients: []string{"QB", "tr"}}
	if !p.AppliesTo("qb") {
		t.Error("应忽略大小写匹配")
	}
	if p.AppliesTo("deluge") {
		t.Error("不在名单中的客户端不应匹配")
	}
}

func TestNormalizeURL(t *testing.T) {
	cases := map[string]string{
		"192.168.1.10:8080":  "http://192.168.1.10:8080",
		"http://a.b:1/":      "http://a.b:1",
		"https://a.b:1":      "https://a.b:1",
		"  http://x.y:2//  ": "http://x.y:2",
	}
	for in, want := range cases {
		if got := NormalizeURL(in); got != want {
			t.Errorf("NormalizeURL(%q) = %q, 期望 %q", in, got, want)
		}
	}
}

// TestEnvOverridesYAML 验证环境变量优先于 YAML，且未设置时 YAML 生效。
//
// 这条规则很重要：Dockerfile 若把 BTG_LISTEN 固定成环境变量，
// 用户在 config.yaml 里改 listen 就会被静默忽略。
// 因此 Dockerfile 刻意不设置这些变量。
func TestEnvOverridesYAML(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	content := "listen: \":7000\"\n" +
		"data_dir: \"/custom\"\n" +
		"clients:\n" +
		"  - name: qb\n" +
		"    type: qbittorrent\n" +
		"    url: \"http://127.0.0.1:8080\"\n" +
		"policies:\n" +
		"  - name: p\n" +
		"    threshold_total: 1000\n" +
		"    limit_total: 100\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	t.Run("未设置环境变量时 YAML 生效", func(t *testing.T) {
		// 确保相关环境变量干净
		for _, k := range []string{"BTG_LISTEN", "BTG_DATA_DIR", "BTG_LOG_LEVEL"} {
			os.Unsetenv(k)
		}
		cfg, _, err := Load(path)
		if err != nil {
			t.Fatalf("加载失败: %v", err)
		}
		if cfg.Listen != ":7000" {
			t.Errorf("listen = %q, 期望 YAML 中的 \":7000\"", cfg.Listen)
		}
		if cfg.DataDir != "/custom" {
			t.Errorf("data_dir = %q, 期望 YAML 中的 \"/custom\"", cfg.DataDir)
		}
	})

	t.Run("设置了环境变量时环境变量优先", func(t *testing.T) {
		t.Setenv("BTG_LISTEN", ":9999")
		cfg, _, err := Load(path)
		if err != nil {
			t.Fatalf("加载失败: %v", err)
		}
		if cfg.Listen != ":9999" {
			t.Errorf("listen = %q, 期望环境变量的 \":9999\"", cfg.Listen)
		}
		// 未被环境变量覆盖的项仍来自 YAML
		if cfg.DataDir != "/custom" {
			t.Errorf("data_dir = %q, 期望仍为 YAML 的 \"/custom\"", cfg.DataDir)
		}
	})
}
