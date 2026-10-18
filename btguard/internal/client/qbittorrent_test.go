package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// qBittorrent API Key 认证（v5.2.0+）
// ---------------------------------------------------------------------------

// qbitStub 是一个最小的假 qBittorrent 服务端，
// 用于验证客户端的认证行为（是否带 Bearer、是否尝试登录）。
type qbitStub struct {
	// version 是上报的版本号。
	version string
	// requireBearer 为 true 时，只有带正确 Authorization 头的请求才放行。
	requireBearer string

	// 记录收到的请求
	loginCalls   int
	bearerSeen   string
	lastAuthHdr  string
	requestPaths []string
}

func (s *qbitStub) handler() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/api/v2/auth/login", func(w http.ResponseWriter, r *http.Request) {
		s.loginCalls++
		w.WriteHeader(403)
		_, _ = w.Write([]byte("Fails."))
	})

	mux.HandleFunc("/api/v2/app/version", func(w http.ResponseWriter, r *http.Request) {
		s.lastAuthHdr = r.Header.Get("Authorization")
		if s.requireBearer != "" && r.Header.Get("Authorization") != "Bearer "+s.requireBearer {
			w.WriteHeader(401)
			return
		}
		s.requestPaths = append(s.requestPaths, r.URL.Path)
		_, _ = w.Write([]byte(s.version))
	})

	mux.HandleFunc("/api/v2/transfer/info", func(w http.ResponseWriter, r *http.Request) {
		s.lastAuthHdr = r.Header.Get("Authorization")
		if s.requireBearer != "" && r.Header.Get("Authorization") != "Bearer "+s.requireBearer {
			w.WriteHeader(401)
			return
		}
		s.requestPaths = append(s.requestPaths, r.URL.Path)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"up_info_speed": 1234,
			"up_info_data":  5678,
			"dl_info_speed": 10,
			"up_rate_limit": 0,
		})
	})

	mux.HandleFunc("/api/v2/sync/maindata", func(w http.ResponseWriter, r *http.Request) {
		s.lastAuthHdr = r.Header.Get("Authorization")
		if s.requireBearer != "" && r.Header.Get("Authorization") != "Bearer "+s.requireBearer {
			w.WriteHeader(401)
			return
		}
		s.requestPaths = append(s.requestPaths, r.URL.Path)
		_, _ = w.Write([]byte(`{"server_state":{},"torrents":{},"peers":{}}`))
	})

	mux.HandleFunc("/api/v2/transfer/setUploadLimit", func(w http.ResponseWriter, r *http.Request) {
		s.lastAuthHdr = r.Header.Get("Authorization")
		if s.requireBearer != "" && r.Header.Get("Authorization") != "Bearer "+s.requireBearer {
			w.WriteHeader(401)
			return
		}
		s.requestPaths = append(s.requestPaths, r.URL.Path)
		_, _ = w.Write([]byte("Ok."))
	})

	return mux
}

// TestAPIKeyUsesBearerAndSkipsLogin 验证配置 API Key 后：
//   - 每个请求都带 Authorization: Bearer
//   - 完全不会调用 auth/login（官方明确 API Key 不能用于 auth 端点）
func TestAPIKeyUsesBearerAndSkipsLogin(t *testing.T) {
	const key = "qbt_testkey1234567890abcdefghij"

	stub := &qbitStub{version: "v5.2.1", requireBearer: key}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewQBittorrent(Options{
		Name:    "qb",
		Type:    "qbittorrent",
		URL:     srv.URL,
		APIKey:  key,
		Timeout: 5 * time.Second,
	})

	if !c.UsesAPIKey() {
		t.Fatal("UsesAPIKey 应为 true")
	}

	st, err := c.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if st.TotalUpSpeed() != 1234 {
		t.Errorf("上传速度 = %d, 期望 1234", st.TotalUpSpeed())
	}

	if stub.loginCalls != 0 {
		t.Errorf("使用 API Key 时不应调用 auth/login，实际调用了 %d 次", stub.loginCalls)
	}
	if stub.lastAuthHdr != "Bearer "+key {
		t.Errorf("Authorization 头 = %q, 期望 %q", stub.lastAuthHdr, "Bearer "+key)
	}
}

// TestNoAPIKeyFallsBackToLogin 验证未配置 API Key 时回退到用户名密码登录。
func TestNoAPIKeyFallsBackToLogin(t *testing.T) {
	stub := &qbitStub{version: "v4.6.5"}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewQBittorrent(Options{
		Name:     "qb",
		Type:     "qbittorrent",
		URL:      srv.URL,
		Username: "admin",
		Password: "adminadmin",
		Timeout:  5 * time.Second,
	})

	if c.UsesAPIKey() {
		t.Fatal("未配置 Key 时 UsesAPIKey 应为 false")
	}

	// 这个假服务端对登录恒返回 403，因此 Fetch 必然失败；
	// 我们只关心它确实尝试了登录。
	_, _ = c.Fetch(context.Background())

	if stub.loginCalls == 0 {
		t.Error("未配置 API Key 时应尝试用户名密码登录")
	}
}

// TestAPIKeyInvalidGivesClearError 验证 Key 无效时给出可操作的错误提示，
// 而不是含糊的 HTTP 状态码。
func TestAPIKeyInvalidGivesClearError(t *testing.T) {
	stub := &qbitStub{version: "v5.2.1", requireBearer: "qbt_correctkey1234567890abcdef"}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewQBittorrent(Options{
		Name:    "qb",
		Type:    "qbittorrent",
		URL:     srv.URL,
		APIKey:  "qbt_wrongkey1234567890abcdefgh",
		Timeout: 5 * time.Second,
	})

	_, err := c.Fetch(context.Background())
	if err == nil {
		t.Fatal("Key 无效时应当返回错误")
	}
	msg := err.Error()
	if !strings.Contains(msg, "API Key") {
		t.Errorf("错误信息应提到 API Key，实际: %s", msg)
	}
	// 不应被误导去重新登录
	if stub.loginCalls != 0 {
		t.Errorf("使用 API Key 时不应尝试登录，实际 %d 次", stub.loginCalls)
	}
}

// TestCheckAuthOnStartupWarnsOnOldVersion 验证：
// 填了 API Key 但版本过低时，给出明确警告而不是静默失败。
func TestCheckAuthOnStartupWarnsOnOldVersion(t *testing.T) {
	stub := &qbitStub{version: "v4.6.5"}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewQBittorrent(Options{
		Name:    "qb",
		Type:    "qbittorrent",
		URL:     srv.URL,
		APIKey:  "qbt_somekey1234567890abcdefgh",
		Timeout: 5 * time.Second,
	})

	msg := c.CheckAuthOnStartup(context.Background())
	if msg == "" {
		t.Fatal("版本过低时应给出警告")
	}
	if !strings.Contains(msg, "5.2") {
		t.Errorf("警告应说明所需版本，实际: %s", msg)
	}
}

// TestCheckAuthOnStartupQuietOnModernVersion 验证版本足够时不产生噪音警告。
func TestCheckAuthOnStartupQuietOnModernVersion(t *testing.T) {
	const key = "qbt_testkey1234567890abcdefghij"
	stub := &qbitStub{version: "v5.2.1", requireBearer: key}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewQBittorrent(Options{
		Name:    "qb",
		Type:    "qbittorrent",
		URL:     srv.URL,
		APIKey:  key,
		Timeout: 5 * time.Second,
	})

	if msg := c.CheckAuthOnStartup(context.Background()); msg != "" {
		t.Errorf("版本足够时不应有警告，实际: %s", msg)
	}
}

// TestVersionAtLeastAPIKeyBoundary 验证 5.2.0 这个版本边界判断正确。
func TestVersionAtLeastAPIKeyBoundary(t *testing.T) {
	cases := []struct {
		v    string
		want bool
	}{
		{"5.2.0", true},
		{"5.2.1", true},
		{"5.2", true},
		{"5.3.0", true},
		{"6.0.0", true},
		{"5.1.4", false},
		{"4.6.5", false},
		{"5.1", false},
	}
	for _, c := range cases {
		if got := versionAtLeast(c.v, 5, 2, 0); got != c.want {
			t.Errorf("versionAtLeast(%q, 5, 2, 0) = %v, 期望 %v", c.v, got, c.want)
		}
	}
}

// TestSetUploadLimitWithAPIKey 验证限速下发同样走 Bearer 认证。
func TestSetUploadLimitWithAPIKey(t *testing.T) {
	const key = "qbt_testkey1234567890abcdefghij"
	stub := &qbitStub{version: "v5.2.1", requireBearer: key}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewQBittorrent(Options{
		Name:    "qb",
		Type:    "qbittorrent",
		URL:     srv.URL,
		APIKey:  key,
		Timeout: 5 * time.Second,
	})

	err := c.SetUploadLimit(context.Background(), SpeedLimit{Total: 1 << 20})
	if err != nil {
		t.Fatalf("下发限速失败: %v", err)
	}
	if stub.lastAuthHdr != "Bearer "+key {
		t.Errorf("限速请求的 Authorization 头 = %q", stub.lastAuthHdr)
	}
}
