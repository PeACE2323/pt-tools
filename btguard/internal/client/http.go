package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"
)

// maxBody 限制单次响应读取量，避免异常服务端撑爆内存。
const maxBody = 8 << 20 // 8 MiB

// ---------------------------------------------------------------------------
// 通用 HTTP 客户端
// ---------------------------------------------------------------------------

// httpDoer 是一个极简 HTTP 封装，负责构造请求、处理认证与解码响应。
// 它被 qBittorrent 与 Transmission 两个实现共用。
type httpDoer struct {
	base   string
	user   string
	pass   string
	client *http.Client
	// basicAuth 为 true 时对每个请求附加 Authorization 头。
	basicAuth bool
	// bearer 非空时对每个请求附加 Authorization: Bearer <bearer>。
	// 这是 qBittorrent v5.2.0+ 的 API Key 认证方式（无状态，无需登录）。
	bearer string
	// referer 用于部分客户端（qBittorrent 相对路径场景）。
	referer string
}

func newHTTPDoer(base, user, pass string, timeout time.Duration, insecure bool, basicAuth bool) *httpDoer {
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	jar, _ := cookiejar.New(nil)
	tr := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		MaxIdleConns:          16,
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       60 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: time.Second,
		ForceAttemptHTTP2:     true,
	}
	if insecure {
		tr.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // 由用户显式开启
	}
	return &httpDoer{
		base:      strings.TrimRight(base, "/"),
		user:      user,
		pass:      pass,
		basicAuth: basicAuth,
		client: &http.Client{
			Transport: tr,
			Timeout:   timeout,
			Jar:       jar,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				if len(via) >= 5 {
					return fmt.Errorf("重定向次数过多")
				}
				return nil
			},
		},
	}
}

// request 发起一次 HTTP 请求并返回响应体。
// 返回的 *http.Response 已读完 body，调用方只需读 data。
func (h *httpDoer) request(ctx context.Context, method, path string, body io.Reader, contentType string, headers map[string]string) ([]byte, int, error) {
	target := h.base + path
	if strings.HasPrefix(path, "http://") || strings.HasPrefix(path, "https://") {
		target = path
	}

	var reqBody io.Reader = body
	if body != nil && contentType == "" {
		contentType = "application/x-www-form-urlencoded"
	}

	req, err := http.NewRequestWithContext(ctx, method, target, reqBody)
	if err != nil {
		return nil, 0, fmt.Errorf("构造请求失败: %w", err)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("User-Agent", "bt-traffic-guard/1.0")
	if h.referer != "" {
		if u, err := url.Parse(h.base); err == nil {
			req.Header.Set("Referer", u.Scheme+"://"+u.Host+h.referer)
			req.Header.Set("Origin", u.Scheme+"://"+u.Host)
		}
	}
	if h.bearer != "" {
		// qBittorrent v5.2.0+ 的 API Key 认证：无状态 Bearer。
		// 优先级高于 Basic Auth。
		req.Header.Set("Authorization", "Bearer "+h.bearer)
	} else if h.basicAuth && h.user != "" {
		req.SetBasicAuth(h.user, h.pass)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := h.client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("请求 %s 失败: %w", target, err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(io.LimitReader(resp.Body, maxBody))
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("读取响应失败: %w", err)
	}
	return data, resp.StatusCode, nil
}

// get 发起 GET 请求。
func (h *httpDoer) get(ctx context.Context, path string) ([]byte, int, error) {
	return h.request(ctx, http.MethodGet, path, nil, "", nil)
}

// requestWithHeaders 与 request 相同，但额外返回指定响应头的值。
// 用于 Transmission 从 409 响应中取出 X-Transmission-Session-Id。
func (h *httpDoer) requestWithHeaders(ctx context.Context, method, path string, body io.Reader,
	contentType string, headers map[string]string, captureHeaders []string) (data []byte, code int, captured string) {

	target := h.base + path
	if strings.HasPrefix(path, "http://") || strings.HasPrefix(path, "https://") {
		target = path
	}
	if body != nil && contentType == "" {
		contentType = "application/x-www-form-urlencoded"
	}

	req, err := http.NewRequestWithContext(ctx, method, target, body)
	if err != nil {
		return nil, 0, ""
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("User-Agent", "bt-traffic-guard/1.0")
	if h.bearer != "" {
		req.Header.Set("Authorization", "Bearer "+h.bearer)
	} else if h.basicAuth && h.user != "" {
		req.SetBasicAuth(h.user, h.pass)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := h.client.Do(req)
	if err != nil {
		return nil, 0, ""
	}
	defer resp.Body.Close()

	// 按传入顺序查找目标响应头，取第一个命中的。
	for _, name := range captureHeaders {
		if v := resp.Header.Get(name); v != "" {
			captured = v
			break
		}
	}
	// 同时检查 trailer 之外的所有头（部分反向代理用小写）。
	if captured == "" {
		for k, vs := range resp.Header {
			for _, name := range captureHeaders {
				if strings.EqualFold(k, name) && len(vs) > 0 {
					captured = vs[0]
				}
			}
		}
	}

	body2, err := io.ReadAll(io.LimitReader(resp.Body, maxBody))
	if err != nil {
		return nil, resp.StatusCode, captured
	}
	return body2, resp.StatusCode, captured
}

// postForm 以表单形式 POST。
func (h *httpDoer) postForm(ctx context.Context, path string, form url.Values, headers map[string]string) ([]byte, int, error) {
	return h.request(ctx, http.MethodPost, path, strings.NewReader(form.Encode()),
		"application/x-www-form-urlencoded", headers)
}

// postJSON 以 JSON 形式 POST。
func (h *httpDoer) postJSON(ctx context.Context, path string, payload any) ([]byte, int, error) {
	buf, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, fmt.Errorf("序列化请求失败: %w", err)
	}
	return h.request(ctx, http.MethodPost, path, bytes.NewReader(buf), "application/json", nil)
}

// statusError 把非 2xx 状态码转换为带片段信息的错误。
func statusError(op string, code int, data []byte) error {
	snippet := strings.TrimSpace(string(data))
	if len(snippet) > 200 {
		snippet = snippet[:200] + "..."
	}
	if snippet == "" {
		return fmt.Errorf("%s: HTTP %d", op, code)
	}
	return fmt.Errorf("%s: HTTP %d (%s)", op, code, snippet)
}

// ---------------------------------------------------------------------------
// 简易重试
// ---------------------------------------------------------------------------

// retry 在遇到临时性错误时重试若干次。
func retry(ctx context.Context, attempts int, delay time.Duration, fn func() error) error {
	if attempts < 1 {
		attempts = 1
	}
	var lastErr error
	for i := 0; i < attempts; i++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		if lastErr = fn(); lastErr == nil {
			return nil
		}
		if i < attempts-1 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay * time.Duration(i+1)):
			}
		}
	}
	return lastErr
}

// ---------------------------------------------------------------------------
// 族分类助手
// ---------------------------------------------------------------------------

// coerceInt64 把 JSON 中的数字（可能是 float64 / json.Number / string）转成 int64。
func coerceInt64(v any) int64 {
	switch t := v.(type) {
	case nil:
		return 0
	case float64:
		return int64(t)
	case float32:
		return int64(t)
	case int:
		return int64(t)
	case int64:
		return t
	case json.Number:
		n, _ := t.Int64()
		return n
	case string:
		var n int64
		_, _ = fmt.Sscanf(strings.TrimSpace(t), "%d", &n)
		return n
	case bool:
		if t {
			return 1
		}
		return 0
	default:
		return 0
	}
}

// coerceBool 把 JSON 中的布尔值（可能是 bool / 数字）转成 bool。
func coerceBool(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case float64:
		return t != 0
	case int:
		return t != 0
	case int64:
		return t != 0
	case string:
		s := strings.ToLower(strings.TrimSpace(t))
		return s == "true" || s == "1" || s == "yes"
	default:
		return false
	}
}

// ---------------------------------------------------------------------------
// 限速状态记录
// ---------------------------------------------------------------------------

// limitTracker 记录我们「最后一次下发」的限速值，
// 以便在恢复时区分「是我们改的」还是「用户自己改的」。
type limitTracker struct {
	mu       sync.Mutex
	applied  SpeedLimit
	hasApply bool
}

// record 记录一次成功下发的限速。
func (l *limitTracker) record(lim SpeedLimit) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.applied = lim
	l.hasApply = true
}

// clear 清除记录。
func (l *limitTracker) clear() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.hasApply = false
}

// applied 返回最近一次下发的限速。
func (l *limitTracker) appliedLimit() (SpeedLimit, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.applied, l.hasApply
}
