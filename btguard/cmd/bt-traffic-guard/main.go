// Command bt-traffic-guard 监控 qBittorrent / Transmission 的上传流量，
// 并在自定义周期内达到阈值时自动限速。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/notify"
	"github.com/btguard/bt-traffic-guard/internal/policy"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
	"github.com/btguard/bt-traffic-guard/internal/web"
)

// version 由构建时通过 -ldflags 注入。
var version = "dev"

// ---------------------------------------------------------------------------
// App：把各部分串起来的运行时容器
// ---------------------------------------------------------------------------

// App 持有全部运行时组件，并支持在不重启进程的情况下热重载配置。
type App struct {
	configPath string

	mu       sync.RWMutex
	cfg      *config.Config
	registry *client.Registry
	monitor  *traffic.Monitor
	engine   *policy.Engine
	notifier *notify.Sender
	loc      *time.Location

	log  *slog.Logger
	logf func(level, format string, args ...any)

	// cfgModTime 用于文件变更检测。
	cfgModTime time.Time
	// lastCfgErr 记录最近一次自动重载失败的原因，避免刷屏。
	lastCfgErr string
}

func main() {
	var (
		configPath = flag.String("config", envOr("BTG_CONFIG", "/config/config.yaml"), "配置文件路径")
		showVer    = flag.Bool("version", false, "打印版本号后退出")
	)
	flag.Parse()

	if *showVer {
		fmt.Println("bt-traffic-guard", version)
		return
	}

	logger := newLogger(envOr("BTG_LOG_LEVEL", "info"))

	app := &App{configPath: *configPath, log: logger}
	app.logf = func(level, format string, args ...any) {
		msg := fmt.Sprintf(format, args...)
		switch level {
		case "debug":
			logger.Debug(msg)
		case "info":
			logger.Info(msg)
		case "warn":
			logger.Warn(msg)
		case "error":
			logger.Error(msg)
		default:
			logger.Info(msg)
		}
	}

	if err := app.Bootstrap(); err != nil {
		logger.Error("启动失败", "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	app.Run(ctx)
	logger.Info("已停止")
}

// Bootstrap 加载配置、构造组件并写入初始配置（若不存在）。
func (a *App) Bootstrap() error {
	cfg, created, err := config.Load(a.configPath)
	if err != nil {
		return err
	}
	if created {
		if err := os.MkdirAll(filepath.Dir(a.configPath), 0o755); err != nil {
			return fmt.Errorf("%w\n\n%s", err, writableHint(filepath.Dir(a.configPath)))
		}
		if err := config.Save(a.configPath, cfg); err != nil {
			return fmt.Errorf("%w\n\n%s", err, writableHint(filepath.Dir(a.configPath)))
		}
		a.log.Info("已生成默认配置文件", "path", a.configPath)
		// 首次启动给出明确的下一步指引。
		// 生成的示例下载器与示例策略都是【未启用】状态，
		// 用户需要先填好地址再启用，否则面板上会是空的。
		a.log.Info("首次使用：打开 Web 面板 →「配置」标签页 → 填写下载器地址与凭据 → 保存")
		a.log.Info("注意：默认的下载器与限速策略都是未启用状态，需要你确认后再启用")
	}
	a.applyConfig(cfg, true)

	if err := os.MkdirAll(cfg.DataDir, 0o755); err != nil {
		a.log.Warn("创建数据目录失败（不影响运行）", "dir", cfg.DataDir, "err", err)
	}
	// 配置文件已存在时，仍要确认它可写：
	// 否则用户在 WebUI 上点保存会失败，而问题要到那时才暴露。
	if created == false {
		if err := checkWritable(a.configPath); err != nil {
			a.log.Warn("配置文件不可写，WebUI 将无法保存配置",
				"path", a.configPath, "err", err)
			a.log.Warn("如需在网页上改配置，请修正权限", "hint", permHint(filepath.Dir(a.configPath)))
		}
	}

	if fi, err := os.Stat(a.configPath); err == nil {
		a.cfgModTime = fi.ModTime()
	}
	return nil
}

// writableHint 生成「目录不可写」时的排查提示。
//
// 容器场景下最常见的原因不是磁盘问题，而是挂载的宿主目录属主与
// 容器内运行用户（默认 uid 1000）不一致 —— 镜像里虽然 chown 过
// /config 与 /data，但 bind mount 会用宿主目录覆盖掉这层权限。
func writableHint(dir string) string {
	return "无法写入 " + dir + "。\n" +
		"如果这是 Docker 容器，常见原因是挂载的宿主目录属主与容器内运行用户不一致。\n" +
		"镜像默认以 uid 1000 运行，请在 NAS 上执行（把路径换成你自己的）：\n" +
		"    sudo chown -R 1000:1000 /path/to/config /path/to/data\n" +
		"或者让容器以 root 运行（在 docker run 中加 --user 0:0，或在 compose 里加 user: \"0:0\"）。"
}

// permHint 生成权限修正提示。
func permHint(dir string) string {
	return "sudo chown -R 1000:1000 " + dir
}

// checkWritable 测试对目标文件（或所在目录）是否可写。
func checkWritable(path string) error {
	f, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	return f.Close()
}

// applyConfig 用新配置重建运行时组件。
// first 为 true 时表示首次启动。
func (a *App) applyConfig(cfg *config.Config, first bool) {
	a.mu.Lock()
	defer a.mu.Unlock()

	a.cfg = cfg
	loc := cfg.Location()
	a.loc = loc

	// --- 重建客户端注册表 ---
	reg := client.NewRegistry()
	for _, cc := range cfg.Clients {
		if !cc.IsEnabled() {
			a.logf("info", "客户端 %s 已禁用，跳过", cc.Name)
			continue
		}
		c, err := client.New(client.Options{
			Name:               cc.Name,
			Type:               cc.Type,
			URL:                cc.URL,
			Username:           cc.Username,
			Password:           cc.Password,
			APIKey:             cc.APIKey,
			InsecureSkipVerify: cc.InsecureSkipVerify,
			Timeout:            cc.Timeout.Std(),
		})
		if err != nil {
			a.logf("error", "构造客户端 %s 失败: %v", cc.Name, err)
			continue
		}
		reg.Add(client.NewManaged(c))
		a.logf("info", "已注册客户端 %s (%s) → %s，认证方式：%s",
			cc.Name, cc.Type, cc.URL, cc.AuthMode())
	}
	a.registry = reg

	// --- 监控器 ---
	prevMonitor := a.monitor
	a.monitor = traffic.NewMonitor(reg, cfg.PollInterval.Std(), cfg.HistoryPoints)
	if prevMonitor != nil && !first {
		a.monitor.AdoptStateFrom(prevMonitor)
	}
	// 让周期累计能跨容器重启延续 —— 升级镜像后不再从 0 重新计数。
	// 状态文件放在数据目录里，随挂载卷一起持久化。
	if cfg.DataDir != "" {
		statePath := filepath.Join(cfg.DataDir, "traffic-state.json")
		a.monitor.SetStatePath(statePath, windowKeyFor(cfg))
	}

	// --- 通知 ---
	host, _ := os.Hostname()
	a.notifier = notify.New(cfg.Notify, host, a.logf)

	// --- 策略引擎 ---
	//
	// 热重载时保留旧的运行状态（周期进度、限速状态、事件记录），
	// 否则用户每改一次配置都会把累计流量清零、把限速解除。
	prevEngine := a.engine
	a.engine = policy.NewEngine(reg, a.monitor, a.notifier, a.logf, loc)
	if prevEngine != nil && !first {
		a.engine.AdoptStateFrom(prevEngine)
	}
	a.engine.Load(cfg.Policies)
	if first {
		a.logf("info", "Traffic Guard %s 启动，监听 %s，采样间隔 %s", version, cfg.Listen, cfg.PollInterval)
	}
}

// Run 启动所有后台循环并阻塞直到 ctx 取消。
func (a *App) Run(ctx context.Context) {
	cfg := a.Config()

	// 1. 采样循环
	//
	// 注意：热重载会替换 a.monitor（连带替换其内部持有的 registry），
	// 所以这里不能只在启动时取一次 a.monitor，否则重载后旧监控器会
	// 继续轮询旧 registry 里的旧客户端，表现为「配置改了但采集还是用旧凭据」。
	// 因此每轮都重新读取当前实例。
	monCtx, cancelMon := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		runMonitorLoop(monCtx, a.currentMonitor)
	}()

	// 2. 策略求值循环（比采样稍慢，留出累计量更新时间）
	// 同样地，每轮都取当前引擎，确保热重载后立即使用新配置。
	wg.Add(1)
	go func() {
		defer wg.Done()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if eng := a.currentEngine(); eng != nil {
					eng.Evaluate(ctx)
				}
			}
		}
	}()

	// 3. 配置文件热重载循环
	wg.Add(1)
	go func() {
		defer wg.Done()
		ticker := time.NewTicker(3 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				a.watchConfig()
			}
		}
	}()

	// 4. HTTP 服务
	srv := &http.Server{Addr: cfg.Listen,
		Handler:           a.buildHandler(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		a.logf("info", "Web UI 已启动: http://0.0.0.0%s", normalizeListen(cfg.Listen))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			a.logf("error", "HTTP 服务异常: %v", err)
			cancelMon()
		}
	}()

	// 5. 客户端认证能力自检（后台执行，不阻塞启动）
	go a.checkClientAuth()

	<-ctx.Done()
	a.logf("info", "收到退出信号，正在关闭…")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		a.logf("warn", "HTTP 服务关闭超时: %v", err)
	}
	cancelMon()
	wg.Wait()

	// 退出前尽力恢复正常限速，避免把用户的下载器留在限速状态。
	a.restoreAllOnExit()
}

// buildHandler 构造带认证的 HTTP handler。
//
// 这里传入的是访问函数而不是实例：配置热重载会替换 registry / monitor / engine，
// 如果在这里固化实例，重载后面板会一直显示旧数据。
func (a *App) buildHandler() http.Handler {
	host, _ := os.Hostname()
	server := web.NewServer(web.Deps{
		RegistryFn: a.currentRegistry,
		MonitorFn:  a.currentMonitor,
		EngineFn:   a.currentEngine,
		ConfigPath: a.configPath,
		Config:     a.Config,
		Reload:     a.Reload,
		UpdateConfig: func(cfg *config.Config) error {
			if err := config.Save(a.configPath, cfg); err != nil {
				return err
			}
			a.applyConfig(cfg, false)
			a.logf("info", "配置已通过 Web UI 更新并生效")
			return nil
		},
		Version:   version,
		StartedAt: time.Now(),
		Hostname:  host,
	})
	return server
}

// currentRegistry 返回当前生效的客户端注册表。
func (a *App) currentRegistry() *client.Registry {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.registry
}

// Config 返回当前生效的配置。
func (a *App) Config() *config.Config {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.cfg
}

// currentMonitor 返回当前生效的监控器（热重载后会变化）。
func (a *App) currentMonitor() *traffic.Monitor {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.monitor
}

// currentEngine 返回当前生效的策略引擎（热重载后会变化）。
func (a *App) currentEngine() *policy.Engine {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.engine
}

// runMonitorLoop 持续采样，并在热重载替换监控器后自动切换到新实例。
//
// 每个监控器实例被切换时会停止使用（它内部的 registry 已过期），
// 但由于它只在被引用的期间工作，旧实例会在本函数切换到新实例后
// 自然失去引用并被回收。
func runMonitorLoop(ctx context.Context, get func() *traffic.Monitor) {
	// 首次采样立即执行，用于尽快建立流量基线。
	var last *traffic.Monitor
	for {
		mon := get()
		if mon == nil {
			select {
			case <-ctx.Done():
				return
			case <-time.After(500 * time.Millisecond):
				continue
			}
		}

		// 监控器被替换了：从头开始一轮（新实例自带空状态，已被 AdoptStateFrom 补齐）
		last = mon
		interval := mon.Interval()
		if interval <= 0 {
			interval = 5 * time.Second
		}

		mon.PollOnce(ctx, time.Now())
		// 采样后落盘周期基线（内部有节流），
		// 使容器重启后周期累计能接着算而不是从 0 开始。
		mon.PersistAfterPoll()

		// 以 interval 为节奏采样，但期间持续检查监控器是否已被替换，
		// 一旦替换立即跳出，避免继续用旧 registry 采样。
		timer := time.NewTimer(interval)
		swapped := false
		for !swapped {
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
				swapped = true
			case <-time.After(300 * time.Millisecond):
				if get() != last {
					swapped = true
				}
			}
		}
		timer.Stop()
	}
}

// Reload 从磁盘重新读取配置并热重载。
func (a *App) Reload() (string, error) {
	cfg, _, err := config.Load(a.configPath)
	if err != nil {
		return "", err
	}
	a.applyConfig(cfg, false)
	return fmt.Sprintf("配置已重载，共 %d 个客户端、%d 条策略", len(cfg.Clients), len(cfg.Policies)), nil
}

// watchConfig 检测配置文件变更并自动重载。
func (a *App) watchConfig() {
	fi, err := os.Stat(a.configPath)
	if err != nil {
		return
	}
	a.mu.RLock()
	last := a.cfgModTime
	a.mu.RUnlock()

	if !fi.ModTime().After(last) {
		return
	}

	a.mu.Lock()
	a.cfgModTime = fi.ModTime()
	a.mu.Unlock()

	cfg, _, err := config.Load(a.configPath)
	if err != nil {
		a.mu.Lock()
		if a.lastCfgErr != err.Error() {
			a.lastCfgErr = err.Error()
			a.mu.Unlock()
			a.logf("error", "配置文件有误，保持原配置运行: %v", err)
			return
		}
		a.mu.Unlock()
		return
	}

	a.mu.Lock()
	a.lastCfgErr = ""
	a.mu.Unlock()

	a.applyConfig(cfg, false)
	a.logf("info", "检测到配置文件变更，已热重载（%d 个客户端，%d 条策略）", len(cfg.Clients), len(cfg.Policies))
}

// restoreAllOnExit 在进程退出时把处于限速状态的客户端恢复为不限速。
func (a *App) restoreAllOnExit() {
	a.mu.RLock()
	engine := a.engine
	reg := a.registry
	a.mu.RUnlock()
	if engine == nil || reg == nil {
		return
	}

	states := engine.States()
	limited := []string{}
	for _, s := range states {
		if s.Limited {
			limited = append(limited, s.Name)
		}
	}
	if len(limited) == 0 {
		return
	}

	a.logf("info", "检测到 %d 条策略处于限速状态，退出前尝试恢复", len(limited))
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	for _, name := range limited {
		if err := engine.ForceResume(ctx, name); err != nil {
			a.logf("warn", "恢复策略 %s 失败: %v", name, err)
		}
	}
}

// checkClientAuth 在后台对支持自检的客户端做一次认证能力检查。
//
// 放在后台是为了不阻塞启动：这一步需要连上客户端拿版本号，
// 客户端暂时不可用时不应当拖慢整个进程的启动。
func (a *App) checkClientAuth() {
	type checker interface {
		CheckAuthOnStartup(ctx context.Context) string
	}

	a.mu.RLock()
	clients := a.registry.All()
	a.mu.RUnlock()

	for _, m := range clients {
		c, ok := m.Client.(checker)
		if !ok {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		msg := c.CheckAuthOnStartup(ctx)
		cancel()
		if msg != "" {
			a.logf("warn", "%s", msg)
		}
	}
}

// ---------------------------------------------------------------------------
// 助手
// ---------------------------------------------------------------------------

// newLogger 构造 slog 日志器。
func newLogger(level string) *slog.Logger {
	var lv slog.Level
	switch strings.ToLower(level) {
	case "debug":
		lv = slog.LevelDebug
	case "warn":
		lv = slog.LevelWarn
	case "error":
		lv = slog.LevelError
	default:
		lv = slog.LevelInfo
	}
	return slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: lv,
		ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr {
			if a.Key == slog.TimeKey {
				a.Value = slog.StringValue(time.Now().Format("2006-01-02 15:04:05"))
			}
			return a
		},
	}))
}

// envOr 返回环境变量值，为空时返回默认值。
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// normalizeListen 把 ":8080" 转成 ":8080" 形式用于展示。
func normalizeListen(addr string) string {
	if strings.HasPrefix(addr, ":") {
		return addr
	}
	if i := strings.LastIndex(addr, ":"); i >= 0 {
		return addr[i:]
	}
	return ":" + addr
}

// windowKeyFor 生成当前周期的标识，用于判断持久化的基线是否仍然有效。
//
// 采用「对齐方式 + 当前周期的起始时刻」作为键：
//   - 同一周期内重启，键不变  -> 恢复基线，周期累计延续
//   - 跨周期后重启，键改变    -> 丢弃旧基线，从头计数
//
// 多条策略若对齐方式不同，取第一条启用策略作为代表 ——
// 对于绝大多数「单一每日/每月策略」的用法已经足够，
// 且比不做持久化要准确得多。
func windowKeyFor(cfg *config.Config) string {
	loc := cfg.Location()
	now := time.Now().In(loc)

	for _, p := range cfg.Policies {
		if !p.IsEnabled() {
			continue
		}
		start, _ := config.WindowBounds(p.Align, p.Window.Std(), now, time.Time{}, loc)
		return p.Align + ":" + start.Format("2006-01-02T15:04")
	}
	// 没有启用的策略：用自然日作为默认周期。
	start, _ := config.WindowBounds("day", 24*time.Hour, now, time.Time{}, loc)
	return "day:" + start.Format("2006-01-02T15:04")
}
