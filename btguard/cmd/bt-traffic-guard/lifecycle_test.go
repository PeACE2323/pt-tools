package main

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// ---------------------------------------------------------------------------
// 测试脚手架
// ---------------------------------------------------------------------------

// silentLogger 返回一个不输出任何内容的 logger。
func silentLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(&discardWriter{}, &slog.HandlerOptions{Level: slog.LevelError}))
}

type discardWriter struct{}

func (discardWriter) Write(p []byte) (int, error) { return len(p), nil }

// newTestApp 构造一个指向临时目录的 App。
func newTestApp(t *testing.T, cfgYAML string) *App {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if cfgYAML != "" {
		if err := os.WriteFile(path, []byte(cfgYAML), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	a := &App{configPath: path, log: silentLogger()}
	a.logf = func(string, string, ...any) {}
	return a
}

// yamlPath 把本地路径转成可安全放进 YAML 双引号字符串的形式。
//
// Windows 路径含反斜杠（C:\Users\...），而 YAML 双引号里的 "\U"
// 是非法转义序列，会让解析直接失败。统一转成正斜杠即可 ——
// Go 的 os 包在 Windows 上也接受正斜杠。
func yamlPath(p string) string {
	return strings.ReplaceAll(p, `\`, `/`)
}

// validYAML 返回一份最小可用配置。
func validYAML(listen string) string {
	return "listen: \"" + listen + "\"\n" +
		"data_dir: \"" + yamlPath(os.TempDir()) + "\"\n" +
		"clients:\n" +
		"  - name: qb\n    type: qbittorrent\n    url: \"http://127.0.0.1:1\"\n" +
		"policies:\n" +
		"  - name: p\n    threshold_total: 1GiB\n    limit_total: 1MiB\n"
}

// ---------------------------------------------------------------------------
// Bootstrap：启动初始化
// ---------------------------------------------------------------------------

// TestBootstrapGeneratesConfigWhenMissing 验证首次启动会生成配置文件。
func TestBootstrapGeneratesConfigWhenMissing(t *testing.T) {
	a := newTestApp(t, "")

	if err := a.Bootstrap(); err != nil {
		t.Fatalf("Bootstrap 失败: %v", err)
	}
	if _, err := os.Stat(a.configPath); err != nil {
		t.Errorf("应生成配置文件: %v", err)
	}
	if a.Config() == nil {
		t.Error("Bootstrap 后配置不应为 nil")
	}
	// 生成的默认配置里，示例客户端与策略都应是禁用的
	cfg := a.Config()
	for _, c := range cfg.Clients {
		if c.IsEnabled() {
			t.Errorf("默认客户端 %q 不应启用", c.Name)
		}
	}
	for _, p := range cfg.Policies {
		if p.IsEnabled() {
			t.Errorf("默认策略 %q 不应启用", p.Name)
		}
	}
}

// TestBootstrapLoadsExistingConfig 验证已存在配置时不会覆盖。
func TestBootstrapLoadsExistingConfig(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))

	if err := a.Bootstrap(); err != nil {
		t.Fatalf("Bootstrap 失败: %v", err)
	}
	if got := a.Config().Listen; got != ":1234" {
		t.Errorf("listen = %q, 期望 :1234（不应被默认值覆盖）", got)
	}
}

// TestBootstrapRejectsInvalidConfig 验证配置非法时启动失败并给出原因。
func TestBootstrapRejectsInvalidConfig(t *testing.T) {
	// 缺少阈值
	bad := "clients:\n  - name: qb\n    type: qbittorrent\n    url: \"http://x:1\"\n" +
		"policies:\n  - name: p\n    limit_total: 1MiB\n"
	a := newTestApp(t, bad)

	err := a.Bootstrap()
	if err == nil {
		t.Fatal("配置非法时应返回错误")
	}
	// 错误信息应指向缺失的阈值字段，便于用户定位
	msg := err.Error()
	if !strings.Contains(msg, "threshold") && !strings.Contains(msg, "阈") {
		t.Errorf("错误信息应说明缺少阈值，实际: %v", msg)
	}
}

// TestBootstrapRegistersEnabledClientsOnly 验证禁用的客户端不会被注册。
func TestBootstrapRegistersEnabledClientsOnly(t *testing.T) {
	yml := "listen: \":1234\"\n" +
		"data_dir: \"" + yamlPath(os.TempDir()) + "\"\n" +
		"clients:\n" +
		"  - name: on\n    type: qbittorrent\n    url: \"http://127.0.0.1:1\"\n    enabled: true\n" +
		"  - name: off\n    type: qbittorrent\n    url: \"http://127.0.0.1:2\"\n    enabled: false\n" +
		"policies:\n  - name: p\n    threshold_total: 1GiB\n    limit_total: 1MiB\n"
	a := newTestApp(t, yml)

	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	names := a.currentRegistry().Names()
	if len(names) != 1 || names[0] != "on" {
		t.Errorf("只应注册启用的客户端，实际: %v", names)
	}
}

// ---------------------------------------------------------------------------
// Reload
// ---------------------------------------------------------------------------

// TestReloadPicksUpChanges 验证重新载入会读取磁盘上的最新配置。
func TestReloadPicksUpChanges(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	// 改写配置
	if err := os.WriteFile(a.configPath, []byte(validYAML(":5678")), 0o644); err != nil {
		t.Fatal(err)
	}

	msg, err := a.Reload()
	if err != nil {
		t.Fatalf("Reload 失败: %v", err)
	}
	if !strings.Contains(msg, "已重载") {
		t.Errorf("返回信息应说明已重载，实际: %s", msg)
	}
	if got := a.Config().Listen; got != ":5678" {
		t.Errorf("listen = %q, 期望 :5678", got)
	}
}

// TestReloadRejectsInvalidConfig 验证非法配置不会破坏运行中的实例。
func TestReloadRejectsInvalidConfig(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(a.configPath, []byte("listen: [broken\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if _, err := a.Reload(); err == nil {
		t.Fatal("非法配置时应返回错误")
	}
	// 关键：原配置必须保持不变
	if got := a.Config().Listen; got != ":1234" {
		t.Errorf("重载失败后配置被破坏: %q", got)
	}
	if a.currentMonitor() == nil {
		t.Error("重载失败后监控器不应失效")
	}
}

// ---------------------------------------------------------------------------
// watchConfig：文件变更自动热重载
// ---------------------------------------------------------------------------

// TestWatchConfigDetectsChange 验证文件变更后自动重载。
func TestWatchConfigDetectsChange(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	// 未变更时不应有任何动作
	a.watchConfig()
	if got := a.Config().Listen; got != ":1234" {
		t.Errorf("无变更时配置不应改变: %q", got)
	}

	// 修改文件并确保 mtime 前进
	time.Sleep(20 * time.Millisecond)
	if err := os.WriteFile(a.configPath, []byte(validYAML(":9999")), 0o644); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(a.configPath, future, future)

	a.watchConfig()
	if got := a.Config().Listen; got != ":9999" {
		t.Errorf("检测到变更后应重载，listen = %q", got)
	}
}

// TestWatchConfigKeepsRunningOnBadConfig 验证配置文件写坏时保持原配置继续运行。
//
// 这很重要：用户用编辑器改配置时可能出现中间态（写了一半），
// 程序不能因此崩溃或把已有配置丢掉。
func TestWatchConfigKeepsRunningOnBadConfig(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(a.configPath, []byte("listen: [broken\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(a.configPath, future, future)

	// 不应 panic，且原配置保持不变
	a.watchConfig()
	if got := a.Config().Listen; got != ":1234" {
		t.Errorf("配置写坏时应保持原配置，实际 listen = %q", got)
	}

	// 反复调用不应刷屏或崩溃（内部有去重）
	for i := 0; i < 3; i++ {
		a.watchConfig()
	}
	if a.Config().Listen != ":1234" {
		t.Error("重复调用后配置被改变")
	}

	// 修好后应能恢复重载
	time.Sleep(20 * time.Millisecond)
	if err := os.WriteFile(a.configPath, []byte(validYAML(":7777")), 0o644); err != nil {
		t.Fatal(err)
	}
	future = time.Now().Add(4 * time.Second)
	_ = os.Chtimes(a.configPath, future, future)
	a.watchConfig()

	if got := a.Config().Listen; got != ":7777" {
		t.Errorf("修复后应能重载，实际 listen = %q", got)
	}
}

// TestWatchConfigSurvivesMissingFile 验证配置文件被删除时不崩溃。
func TestWatchConfigSurvivesMissingFile(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	if err := os.Remove(a.configPath); err != nil {
		t.Fatal(err)
	}
	// 不应 panic；原配置应继续生效
	a.watchConfig()
	if got := a.Config().Listen; got != ":1234" {
		t.Errorf("文件消失后应保持原配置，实际 %q", got)
	}
}

// ---------------------------------------------------------------------------
// buildHandler 与依赖访问器
// ---------------------------------------------------------------------------

// TestBuildHandlerServesRequests 验证构造出的 handler 能正常响应。
func TestBuildHandlerServesRequests(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	h := a.buildHandler()
	if h == nil {
		t.Fatal("buildHandler 不应返回 nil")
	}
	// 健康检查端点最简单，用它确认 handler 可用
	if a.currentRegistry() == nil || a.currentMonitor() == nil || a.currentEngine() == nil {
		t.Error("依赖访问器不应返回 nil")
	}
}

// TestAccessorsReturnCurrentInstances 验证访问器返回的是当前实例。
//
// 这是对一个真实 bug 的回归：热重载会替换 registry/monitor/engine，
// 若访问器返回旧实例，面板会一直显示旧数据。
func TestAccessorsReturnCurrentInstances(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	reg1 := a.currentRegistry()
	mon1 := a.currentMonitor()
	eng1 := a.currentEngine()

	// 触发一次重载
	if _, err := a.Reload(); err != nil {
		t.Fatal(err)
	}

	reg2 := a.currentRegistry()
	mon2 := a.currentMonitor()
	eng2 := a.currentEngine()

	if reg1 == reg2 {
		t.Error("重载后 registry 应被替换")
	}
	if mon1 == mon2 {
		t.Error("重载后 monitor 应被替换")
	}
	if eng1 == eng2 {
		t.Error("重载后 engine 应被替换")
	}
}

// TestReloadPreservesWindowProgress 验证热重载不会丢失周期进度。
func TestReloadPreservesWindowProgress(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	eng1 := a.currentEngine()
	states1 := eng1.States()
	if len(states1) == 0 {
		t.Fatal("应有策略状态")
	}
	end1 := states1[0].WindowEnd

	if _, err := a.Reload(); err != nil {
		t.Fatal(err)
	}

	states2 := a.currentEngine().States()
	if len(states2) == 0 {
		t.Fatal("重载后应有策略状态")
	}
	if !states2[0].WindowEnd.Equal(end1) {
		t.Errorf("热重载不应改变周期边界: %v -> %v", end1, states2[0].WindowEnd)
	}
}

// ---------------------------------------------------------------------------
// restoreAllOnExit：退出时恢复限速
// ---------------------------------------------------------------------------

// TestRestoreAllOnExitResumesLimitedPolicies 验证退出时会解除限速。
//
// 这是重要的用户保护：若容器停止时把用户的下载器留在限速状态，
// 用户会以为「网络变差了」，很难想到是容器退出前没恢复。
func TestRestoreAllOnExitResumesLimitedPolicies(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	eng := a.currentEngine()
	// 手动触发限速
	if err := eng.ForceTrigger(context.Background(), "p"); err != nil {
		t.Fatalf("触发限速失败: %v", err)
	}
	if !eng.States()[0].Limited {
		t.Fatal("前置条件：策略应处于限速状态")
	}

	// 退出时应恢复
	a.restoreAllOnExit()

	if eng.States()[0].Limited {
		t.Error("退出时应解除限速，避免把下载器留在限速状态")
	}
}

// TestRestoreAllOnExitNoopWhenNothingLimited 验证无策略限速时退出流程不报错。
func TestRestoreAllOnExitNoopWhenNothingLimited(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	// 不应 panic
	a.restoreAllOnExit()
}

// ---------------------------------------------------------------------------
// 运行循环
// ---------------------------------------------------------------------------

// TestRunMonitorLoopStopsOnContextCancel 验证采样循环能被正确取消。
func TestRunMonitorLoopStopsOnContextCancel(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		runMonitorLoop(ctx, a.currentMonitor)
	}()

	// 让它跑一会儿，确认能正常工作
	time.Sleep(80 * time.Millisecond)
	cancel()

	select {
	case <-done:
		// 正常退出
	case <-time.After(3 * time.Second):
		t.Fatal("取消 context 后采样循环应及时退出")
	}
}

// TestRunMonitorLoopFollowsReplacement 验证监控器被替换后循环会切换过去。
//
// 这是对一个真实 bug 的回归：热重载会替换 monitor，
// 若采样循环仍持有旧实例，面板会一直显示旧数据，
// 甚至继续用过期凭据访问下载器。
func TestRunMonitorLoopFollowsReplacement(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		runMonitorLoop(ctx, a.currentMonitor)
	}()

	// 等首轮采样完成。
	// 注意不能只 sleep 固定时长：客户端指向的是连不上的地址，
	// 首轮采样要等连接超时，耗时不确定。这里轮询等待。
	mon1 := a.currentMonitor()
	waitFor(t, 5*time.Second, func() bool {
		return mon1.Stats()["poll_count"].(int64) > 0
	}, "首轮应有采样")

	// 替换监控器（模拟热重载）
	if _, err := a.Reload(); err != nil {
		t.Fatal(err)
	}
	mon2 := a.currentMonitor()
	if mon1 == mon2 {
		t.Fatal("前置条件：重载应替换监控器")
	}

	// 等循环切换并开始为新实例采样
	waitFor(t, 5*time.Second, func() bool {
		return mon2.Stats()["poll_count"].(int64) > 0
	}, "采样循环应切换到新监控器，否则热重载后数据会一直是旧的")

	cancel()
	wg.Wait()
}

// waitFor 轮询等待条件成立，超时则判定失败。
//
// 用于等待依赖真实时间的异步行为，避免用固定 sleep 造成偶发失败。
func waitFor(t *testing.T, timeout time.Duration, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Error(msg)
}

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

// TestEnvOr 验证环境变量读取与默认值回退。
func TestEnvOr(t *testing.T) {
	const key = "BTG_TEST_ENVOR_KEY"
	os.Unsetenv(key)

	if got := envOr(key, "fallback"); got != "fallback" {
		t.Errorf("未设置时应返回默认值，实际 %q", got)
	}

	t.Setenv(key, "from-env")
	if got := envOr(key, "fallback"); got != "from-env" {
		t.Errorf("已设置时应返回环境变量值，实际 %q", got)
	}

	// 空值应视为未设置
	t.Setenv(key, "")
	if got := envOr(key, "fallback"); got != "fallback" {
		t.Errorf("空值应回退到默认值，实际 %q", got)
	}
}

// TestNormalizeListen 验证监听地址的展示形式。
func TestNormalizeListen(t *testing.T) {
	cases := map[string]string{
		":8080":          ":8080",
		"0.0.0.0:8080":   ":8080",
		"127.0.0.1:9000": ":9000",
		"8080":           ":8080", // 无冒号时补上
	}
	for in, want := range cases {
		if got := normalizeListen(in); got != want {
			t.Errorf("normalizeListen(%q) = %q, 期望 %q", in, got, want)
		}
	}
}

// TestNewLoggerLevels 验证日志器能按级别构造。
func TestNewLoggerLevels(t *testing.T) {
	for _, lv := range []string{"debug", "info", "warn", "error", "unknown-level"} {
		if l := newLogger(lv); l == nil {
			t.Errorf("级别 %q 应返回非 nil 的 logger", lv)
		}
	}
}

// TestCheckClientAuthHandlesUnreachable 验证客户端不可达时自检不阻塞、不 panic。
func TestCheckClientAuthHandlesUnreachable(t *testing.T) {
	// 指向一个必然连不上的地址
	yml := "listen: \":1234\"\n" +
		"data_dir: \"" + yamlPath(os.TempDir()) + "\"\n" +
		"clients:\n" +
		"  - name: qb\n    type: qbittorrent\n    url: \"http://127.0.0.1:1\"\n" +
		"policies:\n  - name: p\n    threshold_total: 1GiB\n    limit_total: 1MiB\n"
	a := newTestApp(t, yml)
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}

	done := make(chan struct{})
	go func() {
		defer close(done)
		a.checkClientAuth()
	}()

	select {
	case <-done:
		// 正常返回
	case <-time.After(30 * time.Second):
		t.Fatal("自检不应阻塞过久")
	}
}

// TestApplyConfigRebuildsOnChange 验证配置变更会重建运行时组件。
func TestApplyConfigRebuildsOnChange(t *testing.T) {
	a := newTestApp(t, validYAML(":1234"))
	if err := a.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	reg1 := a.currentRegistry()

	cfg2 := a.Config()
	// 深拷贝后改动
	next := *cfg2
	next.Clients = append([]config.ClientConfig(nil), cfg2.Clients...)
	next.Clients = append(next.Clients, config.ClientConfig{
		Name: "second", Type: "qbittorrent", URL: "http://127.0.0.1:3",
	})

	a.applyConfig(&next, false)

	if a.currentRegistry() == reg1 {
		t.Error("配置变更后应重建注册表")
	}
	if got := len(a.currentRegistry().Names()); got != 2 {
		t.Errorf("客户端数 = %d, 期望 2", got)
	}
}
