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
)

// ---------------------------------------------------------------------------
// 测试脚手架
// ---------------------------------------------------------------------------

// configHolder 让测试能够观察到「当前生效的配置」，
// 避免闭包按值捕获导致的陈旧读取。
type configHolder struct {
	cfg *config.Config
}

// newTestServer 构造一个带最小依赖的 Web 服务，供表单接口测试使用。
func newTestServer(t *testing.T, cfg *config.Config) (*Server, *configHolder) {
	t.Helper()

	h := &configHolder{cfg: cfg}
	srv := NewServer(Deps{
		ConfigPath: "/tmp/config.yaml",
		Config:     func() *config.Config { return h.cfg },
		Reload:     func() (string, error) { return "ok", nil },
		UpdateConfig: func(next *config.Config) error {
			// 模拟真实流程：落盘 + 热重载。这里只做校验。
			if err := next.Validate(); err != nil {
				return err
			}
			h.cfg = next
			return nil
		},
		Version:   "test",
		StartedAt: time.Now(),
		Hostname:  "test-host",
	})
	return srv, h
}

// getForm 读取表单视图。
func getForm(t *testing.T, srv *Server) formConfig {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/config/form", nil)
	srv.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("GET /api/config/form 返回 %d: %s", rec.Code, rec.Body.String())
	}
	var out formConfig
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	return out
}

// postForm 提交表单，返回状态码与响应体。
func postForm(t *testing.T, srv *Server, body string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/config/form", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	srv.ServeHTTP(rec, req)

	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

// baseConfig 返回一份用于测试的合法配置。
func baseConfig() *config.Config {
	yes := true
	return &config.Config{
		Listen:        ":8080",
		DataDir:       "/data",
		PollInterval:  config.Duration(5 * time.Second),
		LogLevel:      "info",
		HistoryPoints: 720,
		Clients: []config.ClientConfig{
			{
				Name:     "qb",
				Type:     "qbittorrent",
				URL:      "http://192.168.1.10:8080",
				Username: "admin",
				Password: "secret-pass",
				Enabled:  &yes,
				Timeout:  config.Duration(10 * time.Second),
			},
		},
		Policies: []config.PolicyConfig{
			{
				Name:                "p1",
				Enabled:             &yes,
				Clients:             []string{"*"},
				Window:              config.Duration(24 * time.Hour),
				Align:               "day",
				ThresholdTotal:      config.Bytes(500 * (1 << 30)),
				LimitTotal:          config.Bitrate(1 << 20),
				ResumeOnWindowReset: &yes,
			},
		},
	}
}

// ---------------------------------------------------------------------------
// 敏感字段掩码
// ---------------------------------------------------------------------------

// TestPasswordNeverLeaked 验证 GET 表单接口绝不返回明文密码或 API Key。
//
// 这是安全底线：Web 面板可能在受信网络之外被访问，
// 若把明文凭据直接回传，等于把下载器控制权暴露出去。
func TestPasswordNeverLeaked(t *testing.T) {
	cfg := baseConfig()
	cfg.Clients[0].APIKey = "qbt_abcdefghijklmnopqrstuvwxyz1234"

	srv, _ := newTestServer(t, cfg)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/config/form", nil)
	srv.ServeHTTP(rec, req)

	body := rec.Body.String()
	if strings.Contains(body, "secret-pass") {
		t.Error("响应体泄漏了明文密码")
	}
	if strings.Contains(body, "qbt_abcdefghijklmnopqrstuvwxyz1234") {
		t.Error("响应体泄漏了明文 API Key")
	}
	// 应当返回掩码
	if !strings.Contains(body, "UNCHANGED") {
		t.Error("密码字段应返回掩码占位")
	}
}

// TestAPIKeyHintShown 验证 API Key 以「首尾可见」的提示形式暴露，
// 便于用户确认填的是哪一个 Key，同时不泄漏完整值。
func TestAPIKeyHintShown(t *testing.T) {
	cfg := baseConfig()
	cfg.Clients[0].APIKey = "qbt_abcdefghijklmnopqrstuvwxyz1234"
	srv, _ := newTestServer(t, cfg)

	view := getForm(t, srv)
	if len(view.Clients) != 1 {
		t.Fatalf("客户端数量 = %d, 期望 1", len(view.Clients))
	}
	c := view.Clients[0]
	if !c.HasAPIKey {
		t.Error("HasAPIKey 应为 true")
	}
	if c.APIKeyHint == "" {
		t.Error("应给出 API Key 提示串")
	}
	if strings.Contains(c.APIKeyHint, "qrstuvwxyz") {
		t.Errorf("提示串不应包含完整 Key: %q", c.APIKeyHint)
	}
	if c.AuthMode != "API Key" {
		t.Errorf("认证方式 = %q, 期望 \"API Key\"", c.AuthMode)
	}
}

// ---------------------------------------------------------------------------
// 掩码语义：不修改时不丢密码
// ---------------------------------------------------------------------------

// TestMaskedPasswordPreserved 验证前端原样回传掩码时，
// 已保存的密码不会被清空。这是最容易出错的地方：
// 若把掩码当成真实值写入，用户下次就登录不上了。
func TestMaskedPasswordPreserved(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	if view.Clients[0].Password != passwordMask {
		t.Fatalf("视图中的密码应为掩码，实际 %q", view.Clients[0].Password)
	}

	// 原样回传（模拟用户只改了 URL）
	view.Clients[0].URL = "http://192.168.1.99:8080"
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}

	if h.cfg.Clients[0].Password != "secret-pass" {
		t.Errorf("密码被清空或改写: %q（应为 secret-pass）", h.cfg.Clients[0].Password)
	}
	if h.cfg.Clients[0].URL != "http://192.168.1.99:8080" {
		t.Errorf("URL 未更新: %q", h.cfg.Clients[0].URL)
	}
}

// TestEmptyPasswordClears 验证显式传空串时确实清除密码（用户主动清空）。
func TestEmptyPasswordClears(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients[0].Password = clearToken // 用户主动清空
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}
	if h.cfg.Clients[0].Password != "" {
		t.Errorf("显式清空后密码应为空，实际 %q", h.cfg.Clients[0].Password)
	}
}

// TestEmptyStringKeepsPassword 验证传空串时保留原密码。
//
// 这是防误清空的保护：用户只填了 API Key、没碰密码框时，
// 密码框是空的，此时绝不能把已保存的密码抹掉。
func TestEmptyStringKeepsPassword(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients[0].Password = "" // 空串 = 未修改
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}
	if h.cfg.Clients[0].Password != "secret-pass" {
		t.Errorf("空串应保留原密码，实际 %q", h.cfg.Clients[0].Password)
	}
}

// TestNewAPIKeySaved 验证直接填写新的 API Key 会被保存。
func TestNewAPIKeySaved(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients[0].APIKey = "qbt_newkey1234567890abcdefghij"
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}
	if h.cfg.Clients[0].APIKey != "qbt_newkey1234567890abcdefghij" {
		t.Errorf("API Key 未保存: %q", h.cfg.Clients[0].APIKey)
	}
	if !h.cfg.Clients[0].UsesAPIKey() {
		t.Error("UsesAPIKey 应为 true")
	}
}

// TestAPIKeyClearedByEmpty 验证清空 API Key 后会回退到用户名密码认证。
func TestAPIKeyClearedByEmpty(t *testing.T) {
	cfg := baseConfig()
	cfg.Clients[0].APIKey = "qbt_originalkey1234567890abcde"
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients[0].APIKey = clearToken
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}
	if h.cfg.Clients[0].APIKey != "" {
		t.Errorf("API Key 应被清空，实际 %q", h.cfg.Clients[0].APIKey)
	}
	if h.cfg.Clients[0].AuthMode() != "用户名密码" {
		t.Errorf("应回退到用户名密码认证，实际 %q", h.cfg.Clients[0].AuthMode())
	}
}

// ---------------------------------------------------------------------------
// 增删改
// ---------------------------------------------------------------------------

// TestAddAndRemoveClient 验证通过表单增删下载器。
func TestAddAndRemoveClient(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients = append(view.Clients, clientView{
		Name:     "tr",
		Type:     "transmission",
		URL:      "http://192.168.1.10:9091",
		Enabled:  true,
		Timeout:  "10s",
		Password: "",
	})
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("添加客户端失败 %d: %v", code, resp)
	}
	if len(h.cfg.Clients) != 2 {
		t.Fatalf("客户端数量 = %d, 期望 2", len(h.cfg.Clients))
	}

	// 再删掉第一个
	view2 := getForm(t, srv)
	view2.Clients = view2.Clients[1:]
	body2, _ := json.Marshal(view2)
	code, resp = postForm(t, srv, string(body2))
	if code != 200 {
		t.Fatalf("删除客户端失败 %d: %v", code, resp)
	}
	if len(h.cfg.Clients) != 1 || h.cfg.Clients[0].Name != "tr" {
		t.Errorf("删除结果不正确: %+v", h.cfg.Clients)
	}
}

// TestTransmissionRejectsAPIKey 验证 Transmission 客户端的 API Key 会被丢弃，
// 避免用户误以为它对 Transmission 生效。
func TestTransmissionRejectsAPIKey(t *testing.T) {
	cfg := baseConfig()
	cfg.Clients[0].Type = "transmission"
	cfg.Clients[0].APIKey = ""
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients[0].APIKey = "qbt_shouldbeignored1234567890ab"
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}
	if h.cfg.Clients[0].APIKey != "" {
		t.Errorf("Transmission 的 API Key 应被丢弃，实际 %q", h.cfg.Clients[0].APIKey)
	}
}

// TestEmptyClientNameRejected 验证名称为空时拒绝保存。
func TestEmptyClientNameRejected(t *testing.T) {
	cfg := baseConfig()
	srv, _ := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Clients[0].Name = "   "
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code == 200 {
		t.Fatal("名称为空时应当拒绝保存")
	}
	if msg, _ := resp["error"].(string); !strings.Contains(msg, "名称") {
		t.Errorf("错误信息应说明名称问题，实际: %v", resp)
	}
}

// TestInvalidThresholdRejected 验证非法阈值被拒绝，且配置未被破坏。
func TestInvalidThresholdRejected(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Policies[0].ThresholdTotal = "不是数字"
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code == 200 {
		t.Fatal("非法阈值应当被拒绝")
	}
	if !strings.Contains(strings.ToLower(jsonOf(resp)), "threshold") && !strings.Contains(strings.ToLower(jsonOf(resp)), "阈") {
		t.Errorf("错误信息应提到阈值，实际: %v", resp)
	}
	// 原配置必须保持不变
	if h.cfg.Policies[0].ThTotal() != 500*(1<<30) {
		t.Errorf("校验失败后配置被破坏: %d", h.cfg.Policies[0].ThTotal())
	}
}

// TestPolicyRoundTrip 验证策略在「视图 → 保存 → 再读」后保持等价。
//
// 这一步能抓出单位换算错误（例如 500GiB 被读成 500 字节）。
func TestPolicyRoundTrip(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	p := view.Policies[0]
	if p.ThresholdTotal != "500GiB" {
		t.Errorf("阈值回显 = %q, 期望 500GiB", p.ThresholdTotal)
	}
	if p.LimitTotal != "1MiB" {
		t.Errorf("限速回显 = %q, 期望 1MiB", p.LimitTotal)
	}

	// 改成新值再保存
	view.Policies[0].ThresholdTotal = "1.5TiB"
	view.Policies[0].LimitTotal = "512KiB"
	view.Policies[0].Window = "7d"
	view.Policies[0].Align = "week"
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}

	got := h.cfg.Policies[0]
	wantTh := int64(1.5 * float64(int64(1)<<40))
	if got.ThTotal() != wantTh {
		t.Errorf("阈值 = %d, 期望 %d", got.ThTotal(), wantTh)
	}
	if got.LimTotal() != 512*1024 {
		t.Errorf("限速 = %d, 期望 %d", got.LimTotal(), 512*1024)
	}
	if got.Window.Std() != 7*24*time.Hour {
		t.Errorf("周期 = %v, 期望 168h", got.Window.Std())
	}
	if got.Align != "week" {
		t.Errorf("对齐方式 = %q, 期望 week", got.Align)
	}
}

// TestAddPolicy 验证新增策略。
func TestAddPolicy(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Policies = append(view.Policies, policyView{
		Name:                "new-policy",
		Enabled:             true,
		Clients:             []string{"qb"},
		Window:              "24h",
		Align:               "day",
		ThresholdTotal:      "100GiB",
		LimitTotal:          "512KiB",
		ResumeOnWindowReset: true,
	})
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("添加策略失败 %d: %v", code, resp)
	}
	if len(h.cfg.Policies) != 2 {
		t.Fatalf("策略数量 = %d, 期望 2", len(h.cfg.Policies))
	}
	p := h.cfg.Policies[1]
	if p.ThTotal() != 100*(1<<30) {
		t.Errorf("阈值 = %d", p.ThTotal())
	}
	if p.LimTotal() != 512*1024 {
		t.Errorf("限速 = %d", p.LimTotal())
	}
}

// TestPolicyReferencingUnknownClientRejected 验证引用不存在的客户端会被拒绝。
func TestPolicyReferencingUnknownClientRejected(t *testing.T) {
	cfg := baseConfig()
	srv, _ := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.Policies[0].Clients = []string{"deluge"}
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code == 200 {
		t.Fatal("引用不存在的客户端应当被拒绝")
	}
	if !strings.Contains(jsonOf(resp), "deluge") {
		t.Errorf("错误信息应指出是哪个客户端，实际: %v", resp)
	}
}

// TestGeneralRoundTrip 验证通用设置与通知配置的读写。
func TestGeneralRoundTrip(t *testing.T) {
	cfg := baseConfig()
	srv, h := newTestServer(t, cfg)

	view := getForm(t, srv)
	view.General.PollInterval = "3s"
	view.General.Timezone = "Asia/Shanghai"
	view.General.LogLevel = "debug"
	view.Notify.Enabled = true
	view.Notify.NotifyOn = []string{"speedup", "error"}
	view.Notify.NtfyEnabled = true
	view.Notify.NtfyURL = "https://ntfy.sh"
	view.Notify.NtfyTopic = "my-topic"
	body, _ := json.Marshal(view)

	code, resp := postForm(t, srv, string(body))
	if code != 200 {
		t.Fatalf("保存失败 %d: %v", code, resp)
	}

	if h.cfg.PollInterval.Std() != 3*time.Second {
		t.Errorf("采集间隔 = %v", h.cfg.PollInterval.Std())
	}
	if h.cfg.Timezone != "Asia/Shanghai" {
		t.Errorf("时区 = %q", h.cfg.Timezone)
	}
	if !h.cfg.Notify.Enabled || !h.cfg.Notify.Ntfy.Enabled {
		t.Error("通知未启用")
	}
	if h.cfg.Notify.Ntfy.Topic != "my-topic" {
		t.Errorf("ntfy topic = %q", h.cfg.Notify.Ntfy.Topic)
	}
	if len(h.cfg.Notify.NotifyOn) != 2 {
		t.Errorf("通知事件 = %v", h.cfg.Notify.NotifyOn)
	}
}

// TestMethodNotAllowed 验证不支持的方法被正确拒绝。
func TestMethodNotAllowed(t *testing.T) {
	cfg := baseConfig()
	srv, _ := newTestServer(t, cfg)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/api/config/form", nil)
	srv.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("DELETE 应返回 405，实际 %d", rec.Code)
	}
}

// TestDepsFnPreferredOverInstance 验证 Deps 的访问函数优先于固化实例。
//
// 这是对一个真实 bug 的回归：热重载会整体替换 registry/monitor/engine，
// 早期实现把它们固化在 Deps 里，导致重载后面板一直显示旧数据。
func TestDepsFnPreferredOverInstance(t *testing.T) {
	// 固化一个「旧」实例，并提供一个返回「新」实例的函数
	oldReg := client.NewRegistry()
	oldReg.Add(client.NewManaged(&stubClient{name: "old"}))

	newReg := client.NewRegistry()
	newReg.Add(client.NewManaged(&stubClient{name: "new"}))

	d := Deps{
		Registry:   oldReg,
		RegistryFn: func() *client.Registry { return newReg },
	}

	got := d.registry()
	if got != newReg {
		t.Error("配置了 RegistryFn 时应优先返回函数结果，而不是固化实例")
	}

	// 未提供函数时回退到实例
	d2 := Deps{Registry: oldReg}
	if d2.registry() != oldReg {
		t.Error("未提供 RegistryFn 时应返回固化实例")
	}

	// 函数返回 nil 时不应 panic
	d3 := Deps{RegistryFn: func() *client.Registry { return nil }}
	if d3.registry() != nil {
		t.Error("函数返回 nil 时应如实返回 nil")
	}
}

// stubClient 是满足 client.Client 接口的最小实现。
type stubClient struct{ name string }

func (s *stubClient) Name() string { return s.name }
func (s *stubClient) Type() string { return "stub" }

func (s *stubClient) Fetch(_ context.Context) (client.Stats, error) {
	return client.Stats{Timestamp: time.Now()}, nil
}

func (s *stubClient) SetUploadLimit(_ context.Context, _ client.SpeedLimit) error { return nil }

// jsonOf 把任意值转成 JSON 字符串，便于断言错误信息。
func jsonOf(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
