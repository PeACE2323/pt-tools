// Package notify 负责把策略事件推送到 ntfy 或通用 Webhook。
package notify

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/config"
	"github.com/btguard/bt-traffic-guard/internal/policy"
	"github.com/btguard/bt-traffic-guard/internal/traffic"
)

// Sender 实现 policy.Notifier 接口。
type Sender struct {
	cfg    config.NotifyConfig
	client *http.Client
	logf   func(level, format string, args ...any)
	// hostname 用于在通知中标明来源设备。
	hostname string
}

// New 构造通知发送器。
func New(cfg config.NotifyConfig, hostname string, logf func(level, format string, args ...any)) *Sender {
	if logf == nil {
		logf = func(string, string, ...any) {}
	}
	tr := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		MaxIdleConns:        4,
		IdleConnTimeout:     30 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}
	// 允许推送到自签名 HTTPS 的自建 ntfy。
	tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}

	return &Sender{
		cfg:      cfg,
		hostname: hostname,
		logf:     logf,
		client: &http.Client{
			Transport: tr,
			Timeout:   15 * time.Second,
		},
	}
}

// Notify 按配置发送一条事件通知。
func (s *Sender) Notify(ctx context.Context, ev policy.Event) error {
	if !s.cfg.Enabled {
		return nil
	}
	if !s.cfg.ShouldNotify(string(ev.Type)) {
		return nil
	}

	var errs []string
	if s.cfg.Ntfy.Enabled {
		if err := s.sendNtfy(ctx, ev); err != nil {
			errs = append(errs, "ntfy: "+err.Error())
			s.logf("warn", "ntfy 推送失败: %v", err)
		}
	}
	if s.cfg.Webhook.Enabled {
		if err := s.sendWebhook(ctx, ev); err != nil {
			errs = append(errs, "webhook: "+err.Error())
			s.logf("warn", "webhook 推送失败: %v", err)
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("%s", strings.Join(errs, "; "))
	}
	return nil
}

// title 生成通知标题。
func (s *Sender) title(ev policy.Event) string {
	switch ev.Type {
	case policy.EventSpeedUp:
		return "⏫ BT 上传已达阈值，已限速"
	case policy.EventResume:
		return "✅ BT 上传限速已解除"
	case policy.EventError:
		return "⚠️ Traffic Guard 执行失败"
	default:
		return "Traffic Guard 通知"
	}
}

// priority 生成 ntfy 优先级。
func (s *Sender) priority(ev policy.Event) string {
	if ev.Type == policy.EventError {
		return "high"
	}
	if s.cfg.Ntfy.Priority != "" {
		return s.cfg.Ntfy.Priority
	}
	if ev.Type == policy.EventSpeedUp {
		return "high"
	}
	return "default"
}

// body 生成通知正文。
func (s *Sender) body(ev policy.Event) string {
	var b strings.Builder
	if s.hostname != "" {
		fmt.Fprintf(&b, "设备: %s\n", s.hostname)
	}
	fmt.Fprintf(&b, "策略: %s\n", ev.Policy)
	fmt.Fprintf(&b, "时间: %s\n", ev.Time.Format("2006-01-02 15:04:05"))
	if ev.WindowBytes > 0 && ev.Threshold > 0 {
		fmt.Fprintf(&b, "周期累计: %s / 阈值 %s\n",
			traffic.FormatBytes(ev.WindowBytes), traffic.FormatBytes(ev.Threshold))
	}
	if ev.OldLimit >= 0 {
		fmt.Fprintf(&b, "原限速: %s\n", traffic.FormatSpeed(ev.OldLimit))
	}
	if ev.NewLimit >= 0 {
		fmt.Fprintf(&b, "新限速: %s\n", traffic.FormatSpeed(ev.NewLimit))
	}
	if len(ev.Clients) > 0 {
		fmt.Fprintf(&b, "客户端: %s\n", strings.Join(ev.Clients, ", "))
	}
	fmt.Fprintf(&b, "\n%s", ev.Message)
	return b.String()
}

// sendNtfy 推送到 ntfy。
func (s *Sender) sendNtfy(ctx context.Context, ev policy.Event) error {
	base := strings.TrimRight(s.cfg.Ntfy.URL, "/")
	if base == "" {
		base = "https://ntfy.sh"
	}
	endpoint := base + "/" + s.cfg.Ntfy.Topic

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint,
		strings.NewReader(s.body(ev)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "text/plain; charset=utf-8")
	// ntfy 要求标题使用 ASCII 或做 RFC2047 编码；这里用 ASCII 安全的形式。
	req.Header.Set("Title", asciiTitle(s.title(ev)))
	req.Header.Set("Priority", s.priority(ev))
	req.Header.Set("Tags", ntfyTags(ev.Type))
	if s.cfg.Ntfy.Token != "" {
		req.Header.Set("Authorization", "Bearer "+s.cfg.Ntfy.Token)
	}

	resp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(msg)))
	}
	return nil
}

// sendWebhook 推送到通用 Webhook。
func (s *Sender) sendWebhook(ctx context.Context, ev policy.Event) error {
	method := strings.ToUpper(strings.TrimSpace(s.cfg.Webhook.Method))
	if method == "" {
		method = http.MethodPost
	}

	payload := s.webhookPayload(ev)
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	req, err := http.NewRequestWithContext(ctx, method, s.cfg.Webhook.URL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range s.cfg.Webhook.Headers {
		req.Header.Set(k, v)
	}

	resp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(msg)))
	}
	return nil
}

// webhookPayload 按 format 生成不同结构的请求体。
func (s *Sender) webhookPayload(ev policy.Event) any {
	switch strings.ToLower(s.cfg.Webhook.Format) {
	case "discord":
		// Discord 要求 content 非空，最多 2000 字符。
		content := s.body(ev)
		if len(content) > 1900 {
			content = content[:1900]
		}
		return map[string]any{"content": "**" + s.title(ev) + "**\n```\n" + content + "\n```"}

	case "slack":
		return map[string]any{
			"text": fmt.Sprintf("*%s*\n%s", s.title(ev), s.body(ev)),
		}

	default: // generic
		return map[string]any{
			"event":        string(ev.Type),
			"title":        s.title(ev),
			"policy":       ev.Policy,
			"hostname":     s.hostname,
			"time":         ev.Time.Format(time.RFC3339),
			"message":      ev.Message,
			"clients":      ev.Clients,
			"window_bytes": ev.WindowBytes,
			"threshold":    ev.Threshold,
			"old_limit":    ev.OldLimit,
			"new_limit":    ev.NewLimit,
		}
	}
}

// asciiTitle 把标题转成 ntfy 可安全传输的形式（非 ASCII 用 RFC2047 编码）。
func asciiTitle(s string) string {
	ascii := true
	for _, r := range s {
		if r > 127 {
			ascii = false
			break
		}
	}
	if ascii {
		return s
	}
	// RFC 2047 B 编码（base64）。
	return "=?UTF-8?B?" + base64Std(s) + "?="
}

// ntfyTags 返回事件对应的 emoji 标签。
func ntfyTags(t policy.EventType) string {
	switch t {
	case policy.EventSpeedUp:
		return "arrow_up,fire"
	case policy.EventResume:
		return "white_check_mark"
	case policy.EventError:
		return "warning"
	default:
		return "information_source"
	}
}
