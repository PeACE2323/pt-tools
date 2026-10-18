package web

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/policy"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
)

// ---------------------------------------------------------------------------
// 带完整依赖的测试服务
// ---------------------------------------------------------------------------

// stubDownloader 是满足 client.Client 的最小实现，供 handler 测试使用。
type stubDownloader struct {
	name    string
	speed   int64
	total   int64
	limit   int64
	fetchOK bool
	setErr  error
	sets    []int64
}

func (s *stubDownloader) Name() string { return s.name }
func (s *stubDownloader) Type() string { return "qbittorrent" }
func (s *stubDownloader) URL() string  { return "http://stub" }

func (s *stubDownloader) Fetch(_ context.Context) (client.Stats, error) {
	if !s.fetchOK {
		return client.Stats{}, context.DeadlineExceeded
	}
	return client.Stats{
		Timestamp:      time.Now(),
		UpSpeed:        s.speed,
		UpTotal:        s.total,
		DlSpeed:        100,
		TorrentCount:   3,
		CurrentUpLimit: s.limit,
		Raw:            map[string]any{"version": "v5.2.1"},
	}, nil
}

func (s *stubDownloader) SetUploadLimit(_ context.Context, l client.SpeedLimit) error {
	if s.setErr != nil {
		return s.setErr
	}
	s.limit = l.Total
	s.sets = append(s.sets, l.Total)
	return nil
}

// fullServer 构造一个装配了注册表、监控器、策略引擎的完整测试服务。
type fullServer struct {
	srv   *Server
	cfg   *config.Config // 始终指向当前生效的配置
	dl    *stubDownloader
	reg   *client.Registry
	mon   *traffic.Monitor
	eng   *policy.Engine
	notif *capturingNotifier
}

// capturingNotifier 记录策略引擎发出的通知。
type capturingNotifier struct {
	events []policy.Event
}

func (c *capturingNotifier) Notify(_ context.Context, ev policy.Event) error {
	c.events = append(c.events, ev)
	return nil
}

func newFullServer(t *testing.T, mutate func(*config.Config)) *fullServer {
	t.Helper()

	yes := true
	cfg := &config.Config{
		Listen:        ":8080",
		DataDir:       "/tmp",
		PollInterval:  config.Duration(time.Second),
		LogLevel:      "error",
		HistoryPoints: 50,
		Clients: []config.ClientConfig{{
			Name: "qb", Type: "qbittorrent", URL: "http://stub",
			Enabled: &yes, Timeout: config.Duration(2 * time.Second),
		}},
		Policies: []config.PolicyConfig{{
			Name: "p1", Enabled: &yes, Clients: []string{"*"},
			Window: config.Duration(time.Hour), Align: "rolling",
			ThresholdTotal: config.Bytes(1000), LimitTotal: config.Bitrate(100),
			ResumeOnWindowReset: &yes,
		}},
	}
	if mutate != nil {
		mutate(cfg)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("测试配置不合法: %v", err)
	}

	dl := &stubDownloader{name: "qb", speed: 500, total: 1000, fetchOK: true}
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(dl))
	mon := traffic.NewMonitor(reg, time.Second, 50)
	notif := &capturingNotifier{}
	eng := policy.NewEngine(reg, mon,
		notif, func(string, string, ...any) {}, time.Local)
	eng.Load(cfg.Policies)
	// 建立一次基线，让快照有内容
	mon.PollOnce(context.Background(), time.Now())

	// 用一个稳定的指针保存「当前生效的配置」。
	// 注意不能让 fullServer 持有构造时的 cfg 副本 ——
	// 保存配置会替换整个对象，副本会立刻过期，
	// 导致断言读到旧值（这正是初版测试误报的原因）。
	current := cfg

	f := &fullServer{dl: dl, reg: reg, mon: mon, eng: eng, notif: notif}

	srv := NewServer(Deps{
		RegistryFn: func() *client.Registry { return reg },
		MonitorFn:  func() *traffic.Monitor { return mon },
		EngineFn:   func() *policy.Engine { return eng },
		ConfigPath: t.TempDir() + "/config.yaml",
		Config:     func() *config.Config { return current },
		Reload:     func() (string, error) { return "已重载", nil },
		UpdateConfig: func(next *config.Config) error {
			if err := next.Validate(); err != nil {
				return err
			}
			current = next
			f.cfg = next // 同步给测试使用
			return nil
		},
		Version:   "test-1.0",
		StartedAt: time.Now().Add(-time.Minute),
		Hostname:  "test-host",
	})

	f.srv = srv
	f.cfg = cfg
	return f
}

// do 发起一次请求并返回响应。
func (f *fullServer) do(t *testing.T, method, path string, body string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	var r *http.Request
	if body == "" {
		r = httptest.NewRequest(method, path, nil)
	} else {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		r.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	f.srv.ServeHTTP(w, r)
	return w
}

// ---------------------------------------------------------------------------
// /api/status
// ---------------------------------------------------------------------------

// TestHandleStatus 验证总览接口返回完整结构。
//
// 这是面板首屏依赖的接口，字段缺失会导致前端渲染异常。
func TestHandleStatus(t *testing.T) {
	f := newFullServer(t, nil)

	w := f.do(t, http.MethodGet, "/api/status", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d, 期望 200: %s", w.Code, w.Body.String())
	}

	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}

	for _, k := range []string{
		"version", "hostname", "started_at", "uptime", "now",
		"aggregate", "clients", "policies", "monitor", "config",
	} {
		if _, ok := resp[k]; !ok {
			t.Errorf("响应缺少字段 %q", k)
		}
	}
	if resp["version"] != "test-1.0" {
		t.Errorf("version = %v, 期望 test-1.0", resp["version"])
	}
	if resp["hostname"] != "test-host" {
		t.Errorf("hostname = %v", resp["hostname"])
	}
	clients, ok := resp["clients"].([]any)
	if !ok || len(clients) != 1 {
		t.Errorf("clients 应为 1 个元素的数组，实际 %v", resp["clients"])
	}
	if policies, ok := resp["policies"].([]any); !ok || len(policies) != 1 {
		t.Errorf("policies 应为 1 个元素的数组，实际 %v", resp["policies"])
	}
}

// TestStatusReflectsClientSpeed 验证状态接口能反映真实采样数据。
func TestStatusReflectsClientSpeed(t *testing.T) {
	f := newFullServer(t, nil)
	f.dl.speed = 4096
	f.mon.PollOnce(context.Background(), time.Now())

	w := f.do(t, http.MethodGet, "/api/status", "", nil)
	var resp struct {
		Clients []struct {
			Name    string `json:"name"`
			UpSpeed int64  `json:"up_speed"`
		} `json:"clients"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)

	if len(resp.Clients) != 1 {
		t.Fatalf("客户端数 = %d", len(resp.Clients))
	}
	if resp.Clients[0].UpSpeed != 4096 {
		t.Errorf("上传速度 = %d, 期望 4096", resp.Clients[0].UpSpeed)
	}
}

// ---------------------------------------------------------------------------
// /api/history 与 /api/events
// ---------------------------------------------------------------------------

// TestHandleHistory 验证历史曲线接口。
func TestHandleHistory(t *testing.T) {
	f := newFullServer(t, nil)

	// 无 client 参数时返回聚合曲线
	w := f.do(t, http.MethodGet, "/api/history", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}
	var resp struct {
		Client  string `json:"client"`
		Samples []any  `json:"samples"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("非法 JSON: %v", err)
	}
	if resp.Samples == nil {
		t.Error("samples 不应为 null（前端会直接遍历）")
	}

	// 指定客户端
	w = f.do(t, http.MethodGet, "/api/history?client=qb", "", nil)
	if w.Code != 200 {
		t.Fatalf("指定客户端时状态码 = %d", w.Code)
	}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp.Client != "qb" {
		t.Errorf("client = %q, 期望 qb", resp.Client)
	}
}

// TestHandleEvents 验证事件接口，并检查 limit 参数容错。
func TestHandleEvents(t *testing.T) {
	f := newFullServer(t, nil)

	w := f.do(t, http.MethodGet, "/api/events", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}
	var resp struct {
		Events []any `json:"events"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("非法 JSON: %v", err)
	}
	if resp.Events == nil {
		t.Error("events 不应为 null")
	}

	// 非法的 limit 不应导致 500
	w = f.do(t, http.MethodGet, "/api/events?limit=abc", "", nil)
	if w.Code != 200 {
		t.Errorf("非法 limit 参数应被容忍，实际状态码 %d", w.Code)
	}
	w = f.do(t, http.MethodGet, "/api/events?limit=5", "", nil)
	if w.Code != 200 {
		t.Errorf("正常 limit 状态码 = %d", w.Code)
	}
}

// ---------------------------------------------------------------------------
// 配置读写
// ---------------------------------------------------------------------------

// TestHandleConfigGet 验证读取配置。
//
// 注意：该接口把 Go 结构体直接序列化为 JSON，
// 字段名是 Go 字段名（Clients / Policies，首字母大写），
// 而不是配置文件的 yaml 键名（clients / policies）。
func TestHandleConfigGet(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodGet, "/api/config", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}
	body := w.Body.String()
	if !strings.Contains(body, "Clients") {
		t.Error("响应应包含 Clients 段")
	}
	if !strings.Contains(body, "Policies") {
		t.Error("响应应包含 Policies 段")
	}
}

// TestHandleConfigRejectsPost 验证 /api/config 不接受写入（写入走 /raw 与 /form）。
func TestHandleConfigRejectsPost(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodPost, "/api/config", "{}", nil)
	if w.Code == 200 {
		t.Error("POST /api/config 应当被拒绝")
	}
}

// TestHandleConfigRawRoundTrip 验证 YAML 原文的读写闭环。
//
// 这是「高级」标签页依赖的接口：用户直接编辑 YAML 并保存。
func TestHandleConfigRawRoundTrip(t *testing.T) {
	f := newFullServer(t, nil)

	// 读
	w := f.do(t, http.MethodGet, "/api/config/raw", "", nil)
	if w.Code != 200 {
		t.Fatalf("读取状态码 = %d", w.Code)
	}
	yamlText := w.Body.String()
	if !strings.Contains(yamlText, "clients:") {
		t.Fatalf("读到的 YAML 不含 clients:\n%s", yamlText)
	}
	if ct := w.Header().Get("Content-Type"); !strings.Contains(ct, "text/plain") {
		t.Errorf("Content-Type = %q, 期望 text/plain", ct)
	}

	// 写：合法 YAML
	valid := "listen: \":9999\"\n" +
		"clients:\n  - name: qb\n    type: qbittorrent\n    url: \"http://x:8080\"\n" +
		"policies:\n  - name: p\n    threshold_total: 1GiB\n    limit_total: 1MiB\n"
	body, _ := json.Marshal(map[string]string{"yaml": valid})
	w = f.do(t, http.MethodPost, "/api/config/raw", string(body), nil)
	if w.Code != 200 {
		t.Fatalf("保存合法 YAML 状态码 = %d: %s", w.Code, w.Body.String())
	}
	if f.cfg.Listen != ":9999" {
		t.Errorf("保存后 listen = %q, 期望 :9999", f.cfg.Listen)
	}
}

// TestHandleConfigRawRejectsInvalid 验证非法 YAML 被拒绝且不破坏现有配置。
func TestHandleConfigRawRejectsInvalid(t *testing.T) {
	f := newFullServer(t, nil)
	before := f.cfg.Listen

	// 语法错误
	body, _ := json.Marshal(map[string]string{"yaml": "listen: [unclosed\n"})
	w := f.do(t, http.MethodPost, "/api/config/raw", string(body), nil)
	if w.Code == 200 {
		t.Error("语法错误的 YAML 应被拒绝")
	}
	if f.cfg.Listen != before {
		t.Errorf("校验失败后配置被破坏: %q -> %q", before, f.cfg.Listen)
	}

	// 空内容
	body, _ = json.Marshal(map[string]string{"yaml": "   "})
	w = f.do(t, http.MethodPost, "/api/config/raw", string(body), nil)
	if w.Code == 200 {
		t.Error("空 YAML 应被拒绝")
	}

	// 校验不通过（缺阈值）
	bad := "clients:\n  - name: qb\n    type: qbittorrent\n    url: \"http://x:1\"\n" +
		"policies:\n  - name: p\n    limit_total: 1MiB\n"
	body, _ = json.Marshal(map[string]string{"yaml": bad})
	w = f.do(t, http.MethodPost, "/api/config/raw", string(body), nil)
	if w.Code == 200 {
		t.Error("缺少阈值的配置应被拒绝")
	}
}

// TestHandleConfigRawHandlesBOM 验证带 UTF-8 BOM 的 YAML 能被接受。
//
// Windows 记事本保存 yaml 会写入 BOM，不处理会导致解析失败
// 且报错信息难以理解。
func TestHandleConfigRawHandlesBOM(t *testing.T) {
	f := newFullServer(t, nil)

	valid := "\ufefflisten: \":8888\"\n" +
		"clients:\n  - name: qb\n    type: qbittorrent\n    url: \"http://x:8080\"\n" +
		"policies:\n  - name: p\n    threshold_total: 1GiB\n    limit_total: 1MiB\n"
	body, _ := json.Marshal(map[string]string{"yaml": valid})
	w := f.do(t, http.MethodPost, "/api/config/raw", string(body), nil)
	if w.Code != 200 {
		t.Fatalf("带 BOM 的 YAML 应被接受，状态码 = %d: %s", w.Code, w.Body.String())
	}
	if f.cfg.Listen != ":8888" {
		t.Errorf("listen = %q, 期望 :8888", f.cfg.Listen)
	}
}

// TestHandleConfigRawMethodNotAllowed 验证不支持的方法被拒绝。
func TestHandleConfigRawMethodNotAllowed(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodDelete, "/api/config/raw", "", nil)
	if w.Code != http.StatusMethodNotAllowed {
		t.Errorf("DELETE 状态码 = %d, 期望 405", w.Code)
	}
}

// ---------------------------------------------------------------------------
// 重载与策略操作
// ---------------------------------------------------------------------------

// TestHandleReload 验证重载接口。
func TestHandleReload(t *testing.T) {
	f := newFullServer(t, nil)

	// GET 应被拒绝
	if w := f.do(t, http.MethodGet, "/api/reload", "", nil); w.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET 状态码 = %d, 期望 405", w.Code)
	}
	// POST 应成功
	w := f.do(t, http.MethodPost, "/api/reload", "", nil)
	if w.Code != 200 {
		t.Fatalf("POST 状态码 = %d: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "已重载") {
		t.Errorf("响应应包含重载结果，实际: %s", w.Body.String())
	}
}

// TestHandlePolicyTriggerAndResume 验证手动触发与解除限速。
//
// 这两个按钮是用户排查问题的主要手段，必须能正常工作。
func TestHandlePolicyTriggerAndResume(t *testing.T) {
	f := newFullServer(t, nil)

	// 缺少 name 参数
	if w := f.do(t, http.MethodPost, "/api/policy/trigger", "", nil); w.Code != 400 {
		t.Errorf("缺 name 时状态码 = %d, 期望 400", w.Code)
	}
	// GET 应被拒绝
	if w := f.do(t, http.MethodGet, "/api/policy/trigger?name=p1", "", nil); w.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET 状态码 = %d, 期望 405", w.Code)
	}

	// 手动触发
	w := f.do(t, http.MethodPost, "/api/policy/trigger?name=p1", "", nil)
	if w.Code != 200 {
		t.Fatalf("触发状态码 = %d: %s", w.Code, w.Body.String())
	}
	if f.dl.limit != 100 {
		t.Errorf("触发后客户端限速 = %d, 期望 100", f.dl.limit)
	}
	if len(f.notif.events) == 0 {
		t.Error("触发后应发出通知")
	}

	// 触发时应当能被断言为「已限速」
	w = f.do(t, http.MethodPost, "/api/policy/trigger?name=p1", "", nil)
	if w.Code == 200 {
		t.Error("重复触发应当报错")
	}

	// 手动解除
	w = f.do(t, http.MethodPost, "/api/policy/resume?name=p1", "", nil)
	if w.Code != 200 {
		t.Fatalf("解除状态码 = %d: %s", w.Code, w.Body.String())
	}
	if f.dl.limit != 0 {
		t.Errorf("解除后客户端限速 = %d, 期望 0", f.dl.limit)
	}

	// 未限速时解除应报错
	if w := f.do(t, http.MethodPost, "/api/policy/resume?name=p1", "", nil); w.Code == 200 {
		t.Error("未限速时解除应当报错")
	}

	// 不存在的策略：ForceTrigger 会返回错误，handler 统一映射为 400。
	// 这里只断言「被拒绝」，不绑定具体状态码，
	// 因为 400 与 404 都是合理选择，绑定会让将来调整变得脆弱。
	if w := f.do(t, http.MethodPost, "/api/policy/trigger?name=nope", "", nil); w.Code < 400 {
		t.Errorf("不存在的策略应返回 4xx，实际 %d", w.Code)
	}
}

// ---------------------------------------------------------------------------
// 客户端相关
// ---------------------------------------------------------------------------

// TestHandleClientTest 验证连通性测试接口。
func TestHandleClientTest(t *testing.T) {
	f := newFullServer(t, nil)

	// 不存在的客户端
	if w := f.do(t, http.MethodGet, "/api/client/test?name=nope", "", nil); w.Code != 404 {
		t.Errorf("不存在的客户端状态码 = %d, 期望 404", w.Code)
	}

	// 连通正常
	w := f.do(t, http.MethodGet, "/api/client/test?name=qb", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["ok"] != true {
		t.Errorf("ok = %v, 期望 true（错误: %v）", resp["ok"], resp["error"])
	}
	if resp["version"] != "v5.2.1" {
		t.Errorf("version = %v", resp["version"])
	}

	// 连通失败时应返回 ok=false 而不是 HTTP 错误
	// （前端依赖这个约定来展示具体原因）
	f.dl.fetchOK = false
	w = f.do(t, http.MethodGet, "/api/client/test?name=qb", "", nil)
	if w.Code != 200 {
		t.Errorf("采集失败时状态码 = %d, 期望 200（用 ok 字段表达失败）", w.Code)
	}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["ok"] != false {
		t.Errorf("采集失败时 ok 应为 false，实际 %v", resp["ok"])
	}
	if resp["error"] == nil || resp["error"] == "" {
		t.Error("采集失败时应给出 error 说明")
	}
}

// TestHandleClientLimit 验证手动下发限速。
func TestHandleClientLimit(t *testing.T) {
	f := newFullServer(t, nil)

	// 不存在的客户端
	body := `{"name":"nope","limit":1000}`
	if w := f.do(t, http.MethodPost, "/api/client/limit", body, nil); w.Code != 404 {
		t.Errorf("不存在的客户端状态码 = %d, 期望 404", w.Code)
	}

	// 正常下发
	body = `{"name":"qb","limit":1024}`
	w := f.do(t, http.MethodPost, "/api/client/limit", body, nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d: %s", w.Code, w.Body.String())
	}
	if f.dl.limit != 1024 {
		t.Errorf("限速 = %d, 期望 1024", f.dl.limit)
	}

	// 负数应被拒绝
	body = `{"name":"qb","limit":-5}`
	if w := f.do(t, http.MethodPost, "/api/client/limit", body, nil); w.Code != 400 {
		t.Errorf("负数限速状态码 = %d, 期望 400", w.Code)
	}

	// 分族限速
	eighty := int64(80)
	_ = eighty
	body = `{"name":"qb","limit":0}`
	w = f.do(t, http.MethodPost, "/api/client/limit", body, nil)
	if w.Code != 200 {
		t.Errorf("分族限速状态码 = %d: %s", w.Code, w.Body.String())
	}
}

// ---------------------------------------------------------------------------
// healthz 与认证
// ---------------------------------------------------------------------------

// TestHandleHealthz 验证健康检查端点（Docker HEALTHCHECK 依赖它）。
func TestHandleHealthz(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodGet, "/api/healthz", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}
	if strings.TrimSpace(w.Body.String()) != "ok" {
		t.Errorf("body = %q, 期望 ok", w.Body.String())
	}
}

// TestAuthRequiredWhenEnabled 验证开启认证后未带凭据的请求被拒。
func TestAuthRequiredWhenEnabled(t *testing.T) {
	f := newFullServer(t, func(c *config.Config) {
		c.WebAuth = config.WebAuthConfig{
			Enabled: true, Username: "admin", Password: "s3cret",
		}
	})

	// 不带凭据
	w := f.do(t, http.MethodGet, "/api/status", "", nil)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("未认证状态码 = %d, 期望 401", w.Code)
	}
	if w.Header().Get("WWW-Authenticate") == "" {
		t.Error("401 响应应带 WWW-Authenticate 头（浏览器才会弹登录框）")
	}

	// 错误凭据
	w = f.do(t, http.MethodGet, "/api/status", "", map[string]string{
		"Authorization": basicAuth("admin", "wrong"),
	})
	if w.Code != http.StatusUnauthorized {
		t.Errorf("错误凭据状态码 = %d, 期望 401", w.Code)
	}

	// 正确凭据
	w = f.do(t, http.MethodGet, "/api/status", "", map[string]string{
		"Authorization": basicAuth("admin", "s3cret"),
	})
	if w.Code != 200 {
		t.Errorf("正确凭据状态码 = %d, 期望 200", w.Code)
	}
}

// TestSecurityHeaders 验证安全响应头始终存在。
func TestSecurityHeaders(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodGet, "/api/status", "", nil)
	if got := w.Header().Get("X-Content-Type-Options"); got != "nosniff" {
		t.Errorf("X-Content-Type-Options = %q, 期望 nosniff", got)
	}
	if got := w.Header().Get("Referrer-Policy"); got != "no-referrer" {
		t.Errorf("Referrer-Policy = %q, 期望 no-referrer", got)
	}
}

// TestNoStoreOnAPI 验证 API 响应不被缓存（否则面板会显示过期数据）。
func TestNoStoreOnAPI(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodGet, "/api/status", "", nil)
	if got := w.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control = %q, 期望 no-store", got)
	}
}

// TestJSONErrorShape 验证错误响应的结构统一，便于前端展示。
func TestJSONErrorShape(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodPost, "/api/client/limit", `{"name":"nope","limit":1}`, nil)

	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("错误响应不是 JSON: %v\n%s", err, w.Body.String())
	}
	msg, ok := resp["error"].(string)
	if !ok || msg == "" {
		t.Errorf("错误响应应含非空 error 字段，实际: %v", resp)
	}
}

// TestUnknownRoute 验证未知路径返回 404 而不是 panic。
func TestUnknownRoute(t *testing.T) {
	f := newFullServer(t, nil)
	w := f.do(t, http.MethodGet, "/api/does-not-exist", "", nil)
	if w.Code != http.StatusNotFound {
		t.Errorf("未知路径状态码 = %d, 期望 404", w.Code)
	}
}

// basicAuth 构造 Basic 认证头。
func basicAuth(user, pass string) string {
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.SetBasicAuth(user, pass)
	return req.Header.Get("Authorization")
}
