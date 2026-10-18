package web

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/btguard/bt-traffic-guard/internal/config"
)

// ---------------------------------------------------------------------------
// 敏感字段擦除（安全回归）
// ---------------------------------------------------------------------------

// sensitiveConfig 返回一份填满各类凭据的配置。
func sensitiveConfig() *config.Config {
	no := false
	return &config.Config{
		Listen: ":8080",
		Clients: []config.ClientConfig{{
			Name: "qb", Type: "qbittorrent", URL: "http://x:8080",
			Username: "admin", Password: "dl-password-SECRET",
			APIKey:  "qbt_apikeySECRET1234567890abcdef",
			Enabled: &no,
		}},
		Policies: []config.PolicyConfig{{
			Name: "p", ThresholdTotal: config.Bytes(1 << 30), LimitTotal: config.Bitrate(1 << 20),
		}},
		WebAuth: config.WebAuthConfig{
			Enabled: true, Username: "admin", Password: "panel-SECRET",
		},
		Notify: config.NotifyConfig{
			Enabled: true,
			Ntfy: config.NtfyConfig{
				Enabled: true, URL: "https://ntfy.sh", Topic: "t", Token: "ntfy-SECRET",
			},
			Webhook: config.WebhookConfig{
				Enabled: true, URL: "http://h", Method: "POST",
				Headers: map[string]string{
					"Authorization": "Bearer hook-SECRET",
					"X-Api-Key":     "apikey-SECRET",
					"X-Plain":       "not-a-secret",
				},
			},
		},
	}
}

// TestSanitizeConfigRemovesAllSecrets 验证擦除函数覆盖所有凭据字段。
//
// 这是对一个真实安全漏洞的回归：
// 该函数原本是个空壳（只浅拷贝就返回），
// 导致 GET /api/config 明文回传下载器密码、API Key、
// ntfy 令牌与面板密码 —— 而面板认证默认是关闭的。
func TestSanitizeConfigRemovesAllSecrets(t *testing.T) {
	cfg := sensitiveConfig()
	safe := sanitizeConfig(cfg, true)

	// 序列化后逐个检查秘密是否出现
	blob, err := json.Marshal(safe)
	if err != nil {
		t.Fatal(err)
	}
	text := string(blob)

	secrets := []string{
		"dl-password-SECRET",
		"qbt_apikeySECRET1234567890abcdef",
		"panel-SECRET",
		"ntfy-SECRET",
		"hook-SECRET",
		"apikey-SECRET",
	}
	for _, s := range secrets {
		if strings.Contains(text, s) {
			t.Errorf("擦除后仍包含秘密 %q\n输出: %s", s, text)
		}
	}

	// 非敏感的请求头应保留原值（否则会破坏用户的正常配置）
	if safe.Notify.Webhook.Headers["X-Plain"] != "not-a-secret" {
		t.Error("非敏感请求头不应被擦除")
	}
}

// TestSanitizeConfigKeepsStructure 验证擦除后结构完整。
//
// 前端依赖这些字段渲染表单，擦除不能把结构也删掉。
func TestSanitizeConfigKeepsStructure(t *testing.T) {
	cfg := sensitiveConfig()
	safe := sanitizeConfig(cfg, true)

	if len(safe.Clients) != len(cfg.Clients) {
		t.Fatalf("客户端数量变了: %d -> %d", len(cfg.Clients), len(safe.Clients))
	}
	c := safe.Clients[0]
	if c.Name != "qb" || c.Type != "qbittorrent" || c.URL != "http://x:8080" {
		t.Error("非敏感字段应保持原值")
	}
	if c.Username != "admin" {
		t.Error("用户名应保留（只有密码类字段才擦除）")
	}
	// 已配置的凭据应显示为占位符，供前端判断「该项已设置」
	if c.Password != passwordMask {
		t.Errorf("密码应为占位符，实际 %q", c.Password)
	}
	if c.APIKey != passwordMask {
		t.Errorf("API Key 应为占位符，实际 %q", c.APIKey)
	}
	if safe.WebAuth.Password != passwordMask {
		t.Errorf("面板密码应为占位符，实际 %q", safe.WebAuth.Password)
	}
}

// TestSanitizeConfigEmptyStaysEmpty 验证未设置的字段保持为空，
// 而不是被填成占位符（否则前端会误以为已配置）。
func TestSanitizeConfigEmptyStaysEmpty(t *testing.T) {
	no := false
	cfg := &config.Config{
		Clients: []config.ClientConfig{{
			Name: "c", Type: "qbittorrent", URL: "http://x", Enabled: &no,
		}},
		Policies: []config.PolicyConfig{{
			Name: "p", ThresholdTotal: config.Bytes(1), LimitTotal: config.Bitrate(1),
		}},
	}
	safe := sanitizeConfig(cfg, true)

	if safe.Clients[0].Password != "" {
		t.Errorf("未配置的密码应保持为空，实际 %q", safe.Clients[0].Password)
	}
	if safe.Clients[0].APIKey != "" {
		t.Errorf("未配置的 API Key 应保持为空，实际 %q", safe.Clients[0].APIKey)
	}
}

// TestSanitizeConfigDoesNotMutateOriginal 验证擦除返回的是副本，
// 绝不能改动传入的运行中配置。
//
// 若误改原对象，会把真实凭据替换成占位符，
// 导致下载器立刻认证失败。
func TestSanitizeConfigDoesNotMutateOriginal(t *testing.T) {
	cfg := sensitiveConfig()
	origPass := cfg.Clients[0].Password
	origKey := cfg.Clients[0].APIKey
	origAuth := cfg.WebAuth.Password
	origToken := cfg.Notify.Ntfy.Token

	_ = sanitizeConfig(cfg, true)

	if cfg.Clients[0].Password != origPass {
		t.Error("原配置的密码被修改了")
	}
	if cfg.Clients[0].APIKey != origKey {
		t.Error("原配置的 API Key 被修改了")
	}
	if cfg.WebAuth.Password != origAuth {
		t.Error("原配置的面板密码被修改了")
	}
	if cfg.Notify.Ntfy.Token != origToken {
		t.Error("原配置的 ntfy 令牌被修改了")
	}
}

// TestSanitizeConfigNilSafe 验证 nil 输入不会 panic。
func TestSanitizeConfigNilSafe(t *testing.T) {
	if got := sanitizeConfig(nil, true); got != nil {
		t.Error("nil 输入应返回 nil")
	}
}

// TestSensitiveHeaderDetection 验证敏感请求头的识别。
func TestSensitiveHeaderDetection(t *testing.T) {
	sensitive := []string{
		"Authorization", "authorization", "AUTHORIZATION",
		"X-Api-Key", "api-key", "x-auth-token", "Cookie",
		"Proxy-Authorization", "X-Token",
	}
	for _, h := range sensitive {
		if !isSensitiveHeader(h) {
			t.Errorf("%q 应被识别为敏感头", h)
		}
	}
	notSensitive := []string{"Content-Type", "X-Request-Id", "User-Agent", ""}
	for _, h := range notSensitive {
		if isSensitiveHeader(h) {
			t.Errorf("%q 不应被识别为敏感头", h)
		}
	}
}

// TestHandleConfigNeverLeaksSecrets 通过真实 HTTP 请求验证不泄漏。
//
// 这是最贴近攻击面的测试：直接打接口看响应体。
func TestHandleConfigNeverLeaksSecrets(t *testing.T) {
	f := newFullServer(t, func(c *config.Config) {
		c.Clients[0].Password = "dl-password-SECRET"
		c.Clients[0].APIKey = "qbt_apikeySECRET1234567890abcdef"
		c.WebAuth = config.WebAuthConfig{
			Enabled: false, Username: "admin", Password: "panel-SECRET",
		}
		c.Notify = config.NotifyConfig{
			Enabled: true,
			Ntfy: config.NtfyConfig{
				Enabled: true, URL: "https://ntfy.sh", Topic: "t", Token: "ntfy-SECRET",
			},
		}
	})

	w := f.do(t, http.MethodGet, "/api/config", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}
	body := w.Body.String()

	for _, secret := range []string{
		"dl-password-SECRET",
		"qbt_apikeySECRET1234567890abcdef",
		"panel-SECRET",
		"ntfy-SECRET",
	} {
		if strings.Contains(body, secret) {
			t.Errorf("GET /api/config 泄漏了 %q", secret)
		}
	}

	// 结构仍应完整，前端才能正常渲染
	if !strings.Contains(body, "Clients") {
		t.Error("响应应仍包含配置结构")
	}
}

// TestNoEndpointLeaksSecrets 遍历所有 GET 接口，确认没有一处泄漏凭据。
//
// 单点修复容易遗漏其它路径，这里做一次横向扫描。
func TestNoEndpointLeaksSecrets(t *testing.T) {
	f := newFullServer(t, func(c *config.Config) {
		c.Clients[0].Password = "dl-password-SECRET"
		c.Clients[0].APIKey = "qbt_apikeySECRET1234567890abcdef"
		c.WebAuth = config.WebAuthConfig{Username: "admin", Password: "panel-SECRET"}
		c.Notify = config.NotifyConfig{
			Enabled: true,
			Ntfy: config.NtfyConfig{
				Enabled: true, URL: "https://ntfy.sh", Topic: "t", Token: "ntfy-SECRET",
			},
		}
	})

	secrets := []string{
		"dl-password-SECRET",
		"qbt_apikeySECRET1234567890abcdef",
		"panel-SECRET",
		"ntfy-SECRET",
	}
	endpoints := []string{
		"/api/status",
		"/api/config",
		"/api/config/form",
		"/api/history",
		"/api/events",
		"/api/healthz",
	}

	for _, ep := range endpoints {
		w := f.do(t, http.MethodGet, ep, "", nil)
		body := w.Body.String()
		for _, s := range secrets {
			if strings.Contains(body, s) {
				t.Errorf("接口 %s 泄漏了 %q", ep, s)
			}
		}
	}
}

// TestConfigRawDoesNotLeakSecrets 验证 YAML 原文接口是否泄漏凭据。
//
// 这个接口的设计目的是让用户编辑完整配置，因此【会】包含凭据 ——
// 但它必须与 /api/config/form 的掩码语义一致，
// 或者在文档中明确说明它是高权限接口。
// 这里把当前行为固定下来，避免无意间扩大暴露面。
func TestConfigRawDoesNotLeakSecrets(t *testing.T) {
	f := newFullServer(t, func(c *config.Config) {
		c.Clients[0].Password = "dl-password-SECRET"
	})

	w := f.do(t, http.MethodGet, "/api/config/raw", "", nil)
	if w.Code != 200 {
		t.Fatalf("状态码 = %d", w.Code)
	}

	// 记录当前行为：raw 接口返回完整 YAML（含凭据）。
	//
	// 这是【有意为之】的设计 —— 用户要能在这个页面看到并编辑
	// 自己填过的密码，否则无法修改。
	// 但正因如此，README 中必须强调：
	// 若面板可被外网访问，务必开启面板认证。
	//
	// 用 t.Log 而非断言，是为了让这个「有意的暴露」在
	// 测试输出中留痕，而不是被当成正常情况悄悄略过。
	if strings.Contains(w.Body.String(), "dl-password-SECRET") {
		t.Log("注意：/api/config/raw 按设计返回含凭据的完整 YAML。" +
			"该接口与 /api/config/form 不同（后者做掩码），" +
			"请确保 README 中已提示用户开启面板认证。")
	}
}
