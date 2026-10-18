// Package web 提供 Web UI 与 JSON API。
package web

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"strings"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/policy"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
)

//go:embed static/*
var staticFS embed.FS

// ---------------------------------------------------------------------------
// 依赖注入
// ---------------------------------------------------------------------------

// Deps 是 Web 层需要的所有依赖。
//
// Registry / Monitor / Engine 提供两种传法：
//   - 直接传实例（Registry / Monitor / Engine），适合测试与静态场景
//   - 传访问函数（XxxFn），适合运行时会热重载替换实例的场景
//
// 生产代码使用后者：热重载会整体替换这些组件，
// 若固化实例，面板会在重载后一直显示旧数据、甚至继续用旧凭据采集。
// 访问函数优先于实例。
type Deps struct {
	Registry *client.Registry
	Monitor  *traffic.Monitor
	Engine   *policy.Engine

	RegistryFn func() *client.Registry
	MonitorFn  func() *traffic.Monitor
	EngineFn   func() *policy.Engine

	// ConfigPath 是配置文件路径，保存配置时使用。
	ConfigPath string
	// Config 返回当前生效的配置。
	Config func() *config.Config
	// Reload 触发一次配置重载，返回重载结果描述。
	Reload func() (string, error)
	// UpdateConfig 保存新配置并热重载。
	UpdateConfig func(cfg *config.Config) error
	// Version 是构建版本号。
	Version string
	// StartedAt 是进程启动时刻。
	StartedAt time.Time
	// Hostname 用于界面展示。
	Hostname string
}

// registry 返回当前生效的客户端注册表。
func (d Deps) registry() *client.Registry {
	if d.RegistryFn != nil {
		return d.RegistryFn()
	}
	return d.Registry
}

// monitor 返回当前生效的监控器。
func (d Deps) monitor() *traffic.Monitor {
	if d.MonitorFn != nil {
		return d.MonitorFn()
	}
	return d.Monitor
}

// engine 返回当前生效的策略引擎。
func (d Deps) engine() *policy.Engine {
	if d.EngineFn != nil {
		return d.EngineFn()
	}
	return d.Engine
}

// Server 是 HTTP 服务。
type Server struct {
	deps Deps
	mux  *http.ServeMux
}

// NewServer 构造 HTTP 服务并注册路由。
func NewServer(deps Deps) *Server {
	s := &Server{deps: deps, mux: http.NewServeMux()}
	s.routes()
	return s
}

// routes 注册所有路由。
func (s *Server) routes() {
	// 静态资源
	sub, err := fs.Sub(staticFS, "static")
	if err == nil {
		s.mux.Handle("/", http.FileServer(http.FS(sub)))
	}

	// API
	s.mux.HandleFunc("/api/status", s.wrap(s.handleStatus))
	s.mux.HandleFunc("/api/history", s.wrap(s.handleHistory))
	s.mux.HandleFunc("/api/events", s.wrap(s.handleEvents))
	s.mux.HandleFunc("/api/config", s.wrap(s.handleConfig))
	s.mux.HandleFunc("/api/config/raw", s.wrap(s.handleConfigRaw))
	s.mux.HandleFunc("/api/config/form", s.wrap(s.handleFormConfig))
	s.mux.HandleFunc("/api/reload", s.wrap(s.handleReload))
	s.mux.HandleFunc("/api/policy/resume", s.wrap(s.handlePolicyResume))
	s.mux.HandleFunc("/api/policy/trigger", s.wrap(s.handlePolicyTrigger))
	s.mux.HandleFunc("/api/client/test", s.wrap(s.handleClientTest))
	s.mux.HandleFunc("/api/client/limit", s.wrap(s.handleClientLimit))
	s.mux.HandleFunc("/api/healthz", s.wrap(s.handleHealthz))
	s.mux.HandleFunc("/api/diagnostics", s.wrap(s.handleDiagnostics))
}

// ServeHTTP 实现 http.Handler，附加认证与通用响应头。
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	cfg := s.deps.Config()
	if cfg != nil && cfg.WebAuth.Enabled {
		user, pass, ok := r.BasicAuth()
		if !ok || user != cfg.WebAuth.Username || pass != cfg.WebAuth.Password {
			w.Header().Set("WWW-Authenticate", `Basic realm="Traffic Guard"`)
			http.Error(w, "需要认证", http.StatusUnauthorized)
			return
		}
	}
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	s.mux.ServeHTTP(w, r)
}

// ---------------------------------------------------------------------------
// 通用助手
// ---------------------------------------------------------------------------

// handlerFunc 是不返回错误的处理函数，便于用 wrap 统一包装。
type handlerFunc func(w http.ResponseWriter, r *http.Request) error

// wrap 把错误统一转换为 JSON 响应。
func (s *Server) wrap(fn handlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := fn(w, r); err != nil {
			writeError(w, err)
		}
	}
}

// writeJSON 输出 JSON 响应。
func writeJSON(w http.ResponseWriter, v any) error {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(v)
}

// apiError 是带状态码的错误。
type apiError struct {
	Code    int    `json:"-"`
	Message string `json:"error"`
}

func (e apiError) Error() string { return e.Message }

// writeError 输出错误响应。
func writeError(w http.ResponseWriter, err error) {
	code := http.StatusInternalServerError
	if ae, ok := err.(apiError); ok {
		code = ae.Code
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}

// decodeJSON 解析请求体。
func decodeJSON(r *http.Request, v any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 4<<20))
	if err := dec.Decode(v); err != nil {
		return apiError{Code: http.StatusBadRequest, Message: "请求体解析失败: " + err.Error()}
	}
	return nil
}

// ---------------------------------------------------------------------------
// 处理函数
// ---------------------------------------------------------------------------

// statusResponse 是 /api/status 的响应。
type statusResponse struct {
	Version   string                   `json:"version"`
	Hostname  string                   `json:"hostname"`
	StartedAt time.Time                `json:"started_at"`
	Uptime    string                   `json:"uptime"`
	Now       time.Time                `json:"now"`
	Aggregate traffic.ClientSnapshot   `json:"aggregate"`
	Clients   []traffic.ClientSnapshot `json:"clients"`
	Policies  []policy.PolicyState     `json:"policies"`
	Monitor   map[string]any           `json:"monitor"`
	Config    statusConfigView         `json:"config"`
}

// statusConfigView 是状态响应中附带的配置摘要。
type statusConfigView struct {
	PollInterval string `json:"poll_interval"`
	Timezone     string `json:"timezone"`
	AuthEnabled  bool   `json:"auth_enabled"`
	NotifyOn     bool   `json:"notify_enabled"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) error {
	cfg := s.deps.Config()
	view := statusConfigView{}
	if cfg != nil {
		view = statusConfigView{
			PollInterval: cfg.PollInterval.String(),
			Timezone:     cfg.Location().String(),
			AuthEnabled:  cfg.WebAuth.Enabled,
			NotifyOn:     cfg.Notify.Enabled,
		}
	}

	states := []policy.PolicyState{}
	if s.deps.engine() != nil {
		states = s.deps.engine().States()
	}

	return writeJSON(w, statusResponse{
		Version:   s.deps.Version,
		Hostname:  s.deps.Hostname,
		StartedAt: s.deps.StartedAt,
		Uptime:    time.Since(s.deps.StartedAt).Round(time.Second).String(),
		Now:       time.Now(),
		Aggregate: s.deps.monitor().Aggregate(),
		Clients:   s.deps.monitor().AllSnapshots(),
		Policies:  states,
		Monitor:   s.deps.monitor().Stats(),
		Config:    view,
	})
}

func (s *Server) handleHistory(w http.ResponseWriter, r *http.Request) error {
	name := r.URL.Query().Get("client")
	return writeJSON(w, map[string]any{
		"client":  name,
		"samples": s.deps.monitor().History(name),
	})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) error {
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if _, err := fmt.Sscanf(v, "%d", &limit); err != nil {
			limit = 100
		}
	}
	if s.deps.engine() == nil {
		return writeJSON(w, map[string]any{"events": []any{}})
	}
	return writeJSON(w, map[string]any{"events": s.deps.engine().Events(limit)})
}

// configResponse 是 /api/config 的响应，附带可选的原始 YAML。
type configResponse struct {
	Config *config.Config `json:"config"`
}

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) error {
	cfg := s.deps.Config()
	if cfg == nil {
		return apiError{Code: 500, Message: "配置未初始化"}
	}
	if r.Method == http.MethodGet {
		// 必须擦除敏感字段后再返回：
		// 面板认证默认关闭，直接回传原始配置会把下载器密码、
		// API Key、ntfy 令牌与面板密码全部暴露给任何能访问端口的人。
		return writeJSON(w, sanitizeConfig(cfg, true))
	}
	return apiError{Code: http.StatusMethodNotAllowed, Message: "请使用 GET 读取配置"}
}

// handleConfigRaw 返回或保存原始 YAML 文本。
func (s *Server) handleConfigRaw(w http.ResponseWriter, r *http.Request) error {
	switch r.Method {
	case http.MethodGet:
		cfg := s.deps.Config()
		if cfg == nil {
			return apiError{Code: 500, Message: "配置未初始化"}
		}
		raw, err := marshalYAML(cfg)
		if err != nil {
			return err
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, err = w.Write(raw)
		return err

	case http.MethodPost, http.MethodPut:
		var body struct {
			YAML string `json:"yaml"`
		}
		if err := decodeJSON(r, &body); err != nil {
			return err
		}
		if strings.TrimSpace(body.YAML) == "" {
			return apiError{Code: 400, Message: "YAML 内容不能为空"}
		}
		cfg, err := parseYAML(body.YAML)
		if err != nil {
			return apiError{Code: 400, Message: err.Error()}
		}
		if s.deps.UpdateConfig == nil {
			return apiError{Code: 500, Message: "配置更新未启用"}
		}
		if err := s.deps.UpdateConfig(cfg); err != nil {
			return apiError{Code: 400, Message: err.Error()}
		}
		return writeJSON(w, map[string]any{"ok": true, "message": "配置已保存并生效"})

	default:
		return apiError{Code: http.StatusMethodNotAllowed, Message: "不支持的方法"}
	}
}

func (s *Server) handleReload(w http.ResponseWriter, r *http.Request) error {
	if r.Method != http.MethodPost {
		return apiError{Code: http.StatusMethodNotAllowed, Message: "请使用 POST"}
	}
	if s.deps.Reload == nil {
		return apiError{Code: 500, Message: "重载未启用"}
	}
	msg, err := s.deps.Reload()
	if err != nil {
		return apiError{Code: 400, Message: err.Error()}
	}
	return writeJSON(w, map[string]any{"ok": true, "message": msg})
}

func (s *Server) handlePolicyResume(w http.ResponseWriter, r *http.Request) error {
	if r.Method != http.MethodPost {
		return apiError{Code: http.StatusMethodNotAllowed, Message: "请使用 POST"}
	}
	name := r.URL.Query().Get("name")
	if name == "" {
		return apiError{Code: 400, Message: "缺少 name 参数"}
	}
	if err := s.deps.engine().ForceResume(r.Context(), name); err != nil {
		return apiError{Code: 400, Message: err.Error()}
	}
	return writeJSON(w, map[string]any{"ok": true, "message": "已解除策略 " + name + " 的限速"})
}

func (s *Server) handlePolicyTrigger(w http.ResponseWriter, r *http.Request) error {
	if r.Method != http.MethodPost {
		return apiError{Code: http.StatusMethodNotAllowed, Message: "请使用 POST"}
	}
	name := r.URL.Query().Get("name")
	if name == "" {
		return apiError{Code: 400, Message: "缺少 name 参数"}
	}
	if err := s.deps.engine().ForceTrigger(r.Context(), name); err != nil {
		return apiError{Code: 400, Message: err.Error()}
	}
	return writeJSON(w, map[string]any{"ok": true, "message": "已手动触发策略 " + name})
}

// handleClientTest 测试与某个客户端的连通性。
func (s *Server) handleClientTest(w http.ResponseWriter, r *http.Request) error {
	name := r.URL.Query().Get("name")
	c, ok := s.deps.registry().Get(name)
	if !ok {
		return apiError{Code: 404, Message: "未找到客户端 " + name}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()

	st, err := c.Fetch(ctx)
	if err != nil {
		return writeJSON(w, map[string]any{
			"ok":    false,
			"error": err.Error(),
		})
	}
	return writeJSON(w, map[string]any{
		"ok":               true,
		"version":          st.Raw["version"],
		"up_speed":         st.TotalUpSpeed(),
		"up_total":         st.TotalUpBytes(),
		"dl_speed":         st.DlSpeed,
		"torrents":         st.TorrentCount,
		"peers":            st.PeerCount,
		"current_up_limit": st.CurrentUpLimit,
	})
}

// handleClientLimit 手动设置某个客户端的上传限速。
func (s *Server) handleClientLimit(w http.ResponseWriter, r *http.Request) error {
	if r.Method != http.MethodPost {
		return apiError{Code: http.StatusMethodNotAllowed, Message: "请使用 POST"}
	}
	var body struct {
		Name  string `json:"name"`
		Limit int64  `json:"limit"`
	}
	if err := decodeJSON(r, &body); err != nil {
		return err
	}
	c, ok := s.deps.registry().Get(body.Name)
	if !ok {
		return apiError{Code: 404, Message: "未找到客户端 " + body.Name}
	}

	lim := client.SpeedLimit{Total: body.Limit}
	if lim.Total < 0 {
		return apiError{Code: 400, Message: "limit 不能为负"}
	}

	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	if err := c.SetUploadLimit(ctx, lim); err != nil {
		return apiError{Code: 400, Message: err.Error()}
	}
	return writeJSON(w, map[string]any{"ok": true, "message": "限速已下发"})
}

// handleHealthz 是给 Docker healthcheck 用的轻量端点。
func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) error {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, err := w.Write([]byte("ok"))
	return err
}

// handleDiagnostics 暴露计数器的内部状态，用于排查「周期累计不对」这类问题。
//
// 使用方式：浏览器打开 http://<NAS>:8080/api/diagnostics
// 关注每个客户端的：
//
//	window_bytes   —— 计数器里的周期量（真实值）
//	snapshot_window—— 界面显示的值，应与上一项一致
//	last_total     —— 客户端自报累计量的基线
//	has_baseline   —— 是否已建立基线（false 表示还没采到第二轮）
//	resets         —— 客户端重启次数（累计量归零会 +1）
func (s *Server) handleDiagnostics(w http.ResponseWriter, r *http.Request) error {
	return writeJSON(w, map[string]any{
		"monitor":  s.deps.monitor().Diagnostics(),
		"policies": s.deps.engine().States(),
		"now":      time.Now(),
		"server": map[string]any{
			"started_at": s.deps.StartedAt,
			"uptime":     time.Since(s.deps.StartedAt).Round(time.Second).String(),
			"version":    s.deps.Version,
		},
	})
}
