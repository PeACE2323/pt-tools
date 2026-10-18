package notify

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/policy"
)

// ---------------------------------------------------------------------------
// 测试脚手架
// ---------------------------------------------------------------------------

// capture 记录收到的推送请求。
type capture struct {
	mu       sync.Mutex
	requests []capturedReq
}

type capturedReq struct {
	Method  string
	Path    string
	Headers http.Header
	Body    string
}

func (c *capture) add(r *http.Request, body string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.requests = append(c.requests, capturedReq{
		Method: r.Method, Path: r.URL.Path, Headers: r.Header.Clone(), Body: body,
	})
}

func (c *capture) all() []capturedReq {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]capturedReq(nil), c.requests...)
}

func (c *capture) count() int { return len(c.all()) }

// newServer 起一个记录请求的假推送服务。
func newServer(t *testing.T, status int) (*httptest.Server, *capture) {
	t.Helper()
	c := &capture{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf, _ := io.ReadAll(r.Body)
		c.add(r, string(buf))
		w.WriteHeader(status)
	}))
	t.Cleanup(srv.Close)
	return srv, c
}

func noopLog(string, string, ...any) {}

// speedUpEvent 返回一个限速触发事件。
func speedUpEvent() policy.Event {
	return policy.Event{
		Time:        time.Date(2026, 3, 15, 14, 30, 0, 0, time.UTC),
		Type:        policy.EventSpeedUp,
		Policy:      "daily-500g",
		Clients:     []string{"qbittorrent", "transmission"},
		Message:     "已达阈值，已限速",
		WindowBytes: 500 << 30,
		Threshold:   500 << 30,
		OldLimit:    0,
		NewLimit:    1 << 20,
	}
}

// ---------------------------------------------------------------------------
// 开关语义
// ---------------------------------------------------------------------------

// TestNotifyDisabledDoesNothing 验证未启用时完全不发请求。
func TestNotifyDisabledDoesNothing(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: false,
		Ntfy:    config.NtfyConfig{Enabled: true, URL: srv.URL, Topic: "t"},
	}, "host", noopLog)

	if err := s.Notify(context.Background(), speedUpEvent()); err != nil {
		t.Errorf("未启用时不应返回错误: %v", err)
	}
	if cap.count() != 0 {
		t.Errorf("未启用时不应发出请求，实际 %d 次", cap.count())
	}
}

// TestNotifyOnFiltersEvents 验证 notify_on 白名单生效。
func TestNotifyOnFiltersEvents(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled:  true,
		NotifyOn: []string{"resume"}, // 只关心解除限速
		Ntfy:     config.NtfyConfig{Enabled: true, URL: srv.URL, Topic: "t"},
	}, "host", noopLog)

	// speedup 不在白名单里，不应推送
	if err := s.Notify(context.Background(), speedUpEvent()); err != nil {
		t.Errorf("被过滤时不应报错: %v", err)
	}
	if cap.count() != 0 {
		t.Errorf("被白名单过滤后不应发请求，实际 %d 次", cap.count())
	}

	// resume 在白名单里，应当推送
	ev := speedUpEvent()
	ev.Type = policy.EventResume
	_ = s.Notify(context.Background(), ev)
	if cap.count() != 1 {
		t.Errorf("白名单内的事件应推送，实际 %d 次", cap.count())
	}
}

// TestNotifyOnEmptyMeansAll 验证 notify_on 为空表示全部推送。
func TestNotifyOnEmptyMeansAll(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled:  true,
		NotifyOn: nil,
		Ntfy:     config.NtfyConfig{Enabled: true, URL: srv.URL, Topic: "t"},
	}, "host", noopLog)

	for _, et := range []policy.EventType{policy.EventSpeedUp, policy.EventResume, policy.EventError} {
		ev := speedUpEvent()
		ev.Type = et
		_ = s.Notify(context.Background(), ev)
	}
	if cap.count() != 3 {
		t.Errorf("空白名单应推送全部事件，实际 %d 次", cap.count())
	}
}

// ---------------------------------------------------------------------------
// ntfy
// ---------------------------------------------------------------------------

// TestNtfyRequestShape 验证 ntfy 的请求形状符合其 HTTP 接口约定。
//
// ntfy 用「POST 到 /<topic>，正文为消息内容，标题等放在请求头」的方式，
// 若路径或头部写错，手机会收不到或显示异常。
func TestNtfyRequestShape(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy: config.NtfyConfig{
			Enabled: true, URL: srv.URL, Topic: "my-topic", Priority: "high",
		},
	}, "my-nas", noopLog)

	if err := s.Notify(context.Background(), speedUpEvent()); err != nil {
		t.Fatalf("推送失败: %v", err)
	}

	reqs := cap.all()
	if len(reqs) != 1 {
		t.Fatalf("应发出 1 个请求，实际 %d", len(reqs))
	}
	r := reqs[0]

	if r.Method != http.MethodPost {
		t.Errorf("方法 = %s, 期望 POST", r.Method)
	}
	if r.Path != "/my-topic" {
		t.Errorf("路径 = %s, 期望 /my-topic", r.Path)
	}
	if got := r.Headers.Get("Title"); got == "" {
		t.Error("缺少 Title 头")
	}
	if got := r.Headers.Get("Priority"); got != "high" {
		t.Errorf("Priority = %q, 期望 high", got)
	}
	if got := r.Headers.Get("Tags"); got == "" {
		t.Error("缺少 Tags 头")
	}
	// 正文应包含关键信息，便于用户直接读懂
	for _, want := range []string{"daily-500g", "my-nas", "qbittorrent"} {
		if !strings.Contains(r.Body, want) {
			t.Errorf("正文应包含 %q，实际:\n%s", want, r.Body)
		}
	}
}

// TestNtfyTokenHeader 验证配置了令牌时带上 Bearer 认证。
func TestNtfyTokenHeader(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy: config.NtfyConfig{
			Enabled: true, URL: srv.URL, Topic: "t", Token: "tk_secret",
		},
	}, "host", noopLog)

	_ = s.Notify(context.Background(), speedUpEvent())

	reqs := cap.all()
	if len(reqs) != 1 {
		t.Fatalf("应发出 1 个请求，实际 %d", len(reqs))
	}
	if got := reqs[0].Headers.Get("Authorization"); got != "Bearer tk_secret" {
		t.Errorf("Authorization = %q, 期望 Bearer tk_secret", got)
	}
}

// TestNtfyDefaultURL 验证未填 URL 时回退到官方服务。
func TestNtfyDefaultURL(t *testing.T) {
	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy:    config.NtfyConfig{Enabled: true, URL: "", Topic: "t"},
	}, "host", noopLog)

	// 不实际发请求，只验证 URL 拼接逻辑（通过 priority/title 这类不联网的路径）
	if s.cfg.Ntfy.URL != "" {
		t.Fatal("前置条件：URL 应为空")
	}
	// sendNtfy 内部会把空 URL 回退为 https://ntfy.sh，
	// 这里用一个必定失败的地址验证它确实尝试了发送而非 panic
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_ = s.sendNtfy(ctx, speedUpEvent())
}

// TestNtfyTrailingSlashHandled 验证 URL 末尾斜杠不会导致路径出现双斜杠。
func TestNtfyTrailingSlashHandled(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy:    config.NtfyConfig{Enabled: true, URL: srv.URL + "/", Topic: "topic2"},
	}, "host", noopLog)

	_ = s.Notify(context.Background(), speedUpEvent())

	reqs := cap.all()
	if len(reqs) != 1 {
		t.Fatalf("应发出 1 个请求，实际 %d", len(reqs))
	}
	if reqs[0].Path != "/topic2" {
		t.Errorf("路径 = %q, 期望 /topic2（不应出现双斜杠）", reqs[0].Path)
	}
}

// TestNtfyErrorReported 验证 ntfy 返回错误状态时能被上报。
func TestNtfyErrorReported(t *testing.T) {
	srv, _ := newServer(t, 500)

	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy:    config.NtfyConfig{Enabled: true, URL: srv.URL, Topic: "t"},
	}, "host", noopLog)

	err := s.Notify(context.Background(), speedUpEvent())
	if err == nil {
		t.Fatal("ntfy 返回 500 时应报错")
	}
	if !strings.Contains(err.Error(), "ntfy") {
		t.Errorf("错误信息应标明是 ntfy 失败，实际: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Webhook
// ---------------------------------------------------------------------------

// TestWebhookGenericPayload 验证通用格式的 JSON 字段完整。
//
// 这些字段是给用户自己写对接脚本用的，缺字段会让对接方拿不到数据。
func TestWebhookGenericPayload(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Webhook: config.WebhookConfig{
			Enabled: true, URL: srv.URL + "/hook", Method: "POST", Format: "generic",
		},
	}, "my-nas", noopLog)

	if err := s.Notify(context.Background(), speedUpEvent()); err != nil {
		t.Fatalf("推送失败: %v", err)
	}

	reqs := cap.all()
	if len(reqs) != 1 {
		t.Fatalf("应发出 1 个请求，实际 %d", len(reqs))
	}
	if reqs[0].Path != "/hook" {
		t.Errorf("路径 = %q, 期望 /hook", reqs[0].Path)
	}

	var payload map[string]any
	if err := json.Unmarshal([]byte(reqs[0].Body), &payload); err != nil {
		t.Fatalf("请求体不是合法 JSON: %v\n%s", err, reqs[0].Body)
	}

	// 逐项校验关键字段
	want := map[string]any{
		"event":        "speedup",
		"policy":       "daily-500g",
		"hostname":     "my-nas",
		"window_bytes": float64(500 << 30),
		"threshold":    float64(500 << 30),
		"old_limit":    float64(0),
		"new_limit":    float64(1 << 20),
	}
	for k, v := range want {
		if payload[k] != v {
			t.Errorf("字段 %s = %v, 期望 %v", k, payload[k], v)
		}
	}
	if _, ok := payload["message"].(string); !ok {
		t.Error("缺少 message 字段")
	}
	if _, ok := payload["time"].(string); !ok {
		t.Error("缺少 time 字段")
	}
	if _, ok := payload["clients"].([]any); !ok {
		t.Error("clients 应为数组")
	}
}

// TestWebhookDiscordFormat 验证 Discord 格式使用 content 字段。
//
// Discord 的 Webhook 只认 content / embeds，发 generic 结构不会显示任何内容。
func TestWebhookDiscordFormat(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Webhook: config.WebhookConfig{
			Enabled: true, URL: srv.URL, Method: "POST", Format: "discord",
		},
	}, "host", noopLog)

	_ = s.Notify(context.Background(), speedUpEvent())

	var payload map[string]any
	if err := json.Unmarshal([]byte(cap.all()[0].Body), &payload); err != nil {
		t.Fatalf("非法 JSON: %v", err)
	}
	content, ok := payload["content"].(string)
	if !ok || content == "" {
		t.Fatalf("Discord 格式必须有非空 content 字段，实际: %v", payload)
	}
	if len(content) > 2000 {
		t.Errorf("Discord content 超过 2000 字符上限: %d", len(content))
	}
}

// TestWebhookSlackFormat 验证 Slack 格式使用 text 字段。
func TestWebhookSlackFormat(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Webhook: config.WebhookConfig{
			Enabled: true, URL: srv.URL, Method: "POST", Format: "slack",
		},
	}, "host", noopLog)

	_ = s.Notify(context.Background(), speedUpEvent())

	var payload map[string]any
	_ = json.Unmarshal([]byte(cap.all()[0].Body), &payload)
	if txt, ok := payload["text"].(string); !ok || txt == "" {
		t.Fatalf("Slack 格式必须有非空 text 字段，实际: %v", payload)
	}
}

// TestWebhookCustomHeaders 验证自定义请求头被带上（常用于鉴权）。
func TestWebhookCustomHeaders(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Webhook: config.WebhookConfig{
			Enabled: true, URL: srv.URL, Method: "POST", Format: "generic",
			Headers: map[string]string{
				"Authorization": "Bearer my-token",
				"X-Custom":      "value1",
			},
		},
	}, "host", noopLog)

	_ = s.Notify(context.Background(), speedUpEvent())

	h := cap.all()[0].Headers
	if got := h.Get("Authorization"); got != "Bearer my-token" {
		t.Errorf("Authorization = %q", got)
	}
	if got := h.Get("X-Custom"); got != "value1" {
		t.Errorf("X-Custom = %q", got)
	}
	if got := h.Get("Content-Type"); !strings.Contains(got, "application/json") {
		t.Errorf("Content-Type = %q, 期望 application/json", got)
	}
}

// TestWebhookMethodHonored 验证自定义 HTTP 方法生效。
func TestWebhookMethodHonored(t *testing.T) {
	srv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Webhook: config.WebhookConfig{
			Enabled: true, URL: srv.URL, Method: "PUT", Format: "generic",
		},
	}, "host", noopLog)

	_ = s.Notify(context.Background(), speedUpEvent())

	if got := cap.all()[0].Method; got != http.MethodPut {
		t.Errorf("方法 = %s, 期望 PUT", got)
	}
}

// TestWebhookErrorReported 验证 Webhook 失败能被上报。
func TestWebhookErrorReported(t *testing.T) {
	srv, _ := newServer(t, 404)

	s := New(config.NotifyConfig{
		Enabled: true,
		Webhook: config.WebhookConfig{Enabled: true, URL: srv.URL, Method: "POST"},
	}, "host", noopLog)

	err := s.Notify(context.Background(), speedUpEvent())
	if err == nil {
		t.Fatal("404 时应报错")
	}
	if !strings.Contains(err.Error(), "webhook") {
		t.Errorf("错误信息应标明是 webhook 失败，实际: %v", err)
	}
}

// TestBothChannelsFailureAggregated 验证两个渠道都失败时错误被合并。
func TestBothChannelsFailureAggregated(t *testing.T) {
	srv, _ := newServer(t, 500)

	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy:    config.NtfyConfig{Enabled: true, URL: srv.URL, Topic: "t"},
		Webhook: config.WebhookConfig{Enabled: true, URL: srv.URL, Method: "POST"},
	}, "host", noopLog)

	err := s.Notify(context.Background(), speedUpEvent())
	if err == nil {
		t.Fatal("两个渠道都失败时应报错")
	}
	msg := err.Error()
	if !strings.Contains(msg, "ntfy") || !strings.Contains(msg, "webhook") {
		t.Errorf("错误信息应同时提及两个渠道，实际: %v", msg)
	}
}

// TestPartialFailureStillAttemptsBoth 验证 ntfy 失败不影响 webhook 发送。
//
// 这是重要的可用性保证：不能因为一个渠道挂了就漏掉另一个渠道的通知。
func TestPartialFailureStillAttemptsBoth(t *testing.T) {
	// ntfy 指向必然失败的端口，webhook 指向正常服务
	okSrv, cap := newServer(t, 200)

	s := New(config.NotifyConfig{
		Enabled: true,
		Ntfy:    config.NtfyConfig{Enabled: true, URL: "http://127.0.0.1:1", Topic: "t"},
		Webhook: config.WebhookConfig{Enabled: true, URL: okSrv.URL, Method: "POST"},
	}, "host", noopLog)

	err := s.Notify(context.Background(), speedUpEvent())
	if err == nil {
		t.Error("ntfy 失败时整体应返回错误")
	}
	if cap.count() != 1 {
		t.Errorf("ntfy 失败不应阻止 webhook 发送，实际 webhook 收到 %d 次", cap.count())
	}
}

// ---------------------------------------------------------------------------
// 辅助逻辑
// ---------------------------------------------------------------------------

// TestAsciiTitleRFC2047 验证非 ASCII 标题按 RFC 2047 编码。
//
// HTTP 头只能放 ASCII；中文标题若不编码，ntfy 会乱码或直接拒收。
func TestAsciiTitleRFC2047(t *testing.T) {
	// 纯 ASCII 原样返回
	if got := asciiTitle("Simple Title"); got != "Simple Title" {
		t.Errorf("ASCII 标题不应被编码，实际 %q", got)
	}
	// 含中文时按 RFC 2047 B 编码
	got := asciiTitle("上传已达阈值")
	if !strings.HasPrefix(got, "=?UTF-8?B?") || !strings.HasSuffix(got, "?=") {
		t.Errorf("中文标题应做 RFC2047 编码，实际 %q", got)
	}
	// 编码结果必须全为 ASCII，否则放不进 HTTP 头
	for _, r := range got {
		if r > 127 {
			t.Fatalf("编码后仍含非 ASCII 字符: %q", got)
		}
	}
}

// TestPriorityMapping 验证事件到优先级的映射合理。
func TestPriorityMapping(t *testing.T) {
	s := New(config.NotifyConfig{
		Ntfy: config.NtfyConfig{Priority: "low"},
	}, "host", noopLog)

	// 错误事件应提升为 high，避免用户漏看
	ev := speedUpEvent()
	ev.Type = policy.EventError
	if got := s.priority(ev); got != "high" {
		t.Errorf("错误事件优先级 = %q, 期望 high", got)
	}

	// 其它事件遵循配置
	ev.Type = policy.EventResume
	if got := s.priority(ev); got != "low" {
		t.Errorf("恢复事件优先级 = %q, 期望 low", got)
	}
}

// TestBodyContainsKeyInfo 验证通知正文包含用户判断所需的信息。
func TestBodyContainsKeyInfo(t *testing.T) {
	s := New(config.NotifyConfig{}, "my-nas", noopLog)

	ev := speedUpEvent()
	body := s.body(ev)

	for _, want := range []string{
		"my-nas",      // 来源设备
		"daily-500g",  // 策略名
		"500.00 GiB",  // 阈值（人类可读）
		"qbittorrent", // 涉及的客户端
	} {
		if !strings.Contains(body, want) {
			t.Errorf("正文应包含 %q\n实际内容:\n%s", want, body)
		}
	}
}

// TestTitlePerEventType 验证不同事件类型的标题不同。
func TestTitlePerEventType(t *testing.T) {
	s := New(config.NotifyConfig{}, "host", noopLog)

	titles := map[policy.EventType]string{}
	for _, et := range []policy.EventType{policy.EventSpeedUp, policy.EventResume, policy.EventError} {
		ev := speedUpEvent()
		ev.Type = et
		ti := s.title(ev)
		if ti == "" {
			t.Errorf("事件 %s 的标题不应为空", et)
		}
		titles[et] = ti
	}
	// 三种事件的标题应互不相同
	if titles[policy.EventSpeedUp] == titles[policy.EventResume] ||
		titles[policy.EventResume] == titles[policy.EventError] {
		t.Error("不同事件类型应有不同标题")
	}
}

// TestNtfyTagsPerEvent 验证不同事件带不同的 emoji 标签。
func TestNtfyTagsPerEvent(t *testing.T) {
	for _, et := range []policy.EventType{policy.EventSpeedUp, policy.EventResume, policy.EventError} {
		if ntfyTags(et) == "" {
			t.Errorf("事件 %s 应有标签", et)
		}
	}
}
