package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// 假 Transmission RPC 服务端
// ---------------------------------------------------------------------------

// trStub 是一个最小的 Transmission RPC 模拟服务。
//
// 真实 Transmission 的会话协商流程：
//  1. 客户端不带 X-Transmission-Session-Id 发请求
//  2. 服务端返回 409，并在【响应头】里给出当前 session id
//  3. 客户端带上该头重发，才拿到 200
type trStub struct {
	// sessionID 是当前有效的会话 id。
	sessionID string
	// requireAuth 为 true 时校验 Basic Auth。
	requireAuth bool
	user, pass  string

	// 可观测计数
	requests      int32
	conflicts     int32 // 返回 409 的次数
	authFailures  int32
	lastMethod    string
	lastSessionID string
	lastAuthUser  string

	// 上传限速（KB/s），session-set 会修改它
	upLimitKB int64
	altSpeed  bool

	// 速率与累计量
	upSpeed  int64
	uploaded int64
}

func (s *trStub) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&s.requests, 1)

		if s.requireAuth {
			u, p, ok := r.BasicAuth()
			if !ok || u != s.user || p != s.pass {
				atomic.AddInt32(&s.authFailures, 1)
				w.WriteHeader(401)
				return
			}
			s.lastAuthUser = u
		}

		// 会话校验：不带或带错 session id 一律 409
		sid := r.Header.Get("X-Transmission-Session-Id")
		s.lastSessionID = sid
		if sid != s.sessionID {
			atomic.AddInt32(&s.conflicts, 1)
			w.Header().Set("X-Transmission-Session-Id", s.sessionID)
			w.WriteHeader(409)
			return
		}

		var req struct {
			Method    string          `json:"method"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(400)
			return
		}
		s.lastMethod = req.Method

		var args any
		switch req.Method {
		case "session-get":
			args = map[string]any{
				"version":                "4.0.5",
				"rpc-version":            17,
				"speed-limit-up":         s.upLimitKB,
				"speed-limit-up-enabled": s.upLimitKB > 0,
				"alt-speed-enabled":      s.altSpeed,
				"alt-speed-up":           0,
			}
		case "session-stats":
			args = map[string]any{
				"activeTorrentCount": 2,
				"downloadSpeed":      1024,
				"uploadSpeed":        s.upSpeed,
				"pausedTorrentCount": 0,
				"torrentCount":       3,
				"cumulative-stats": map[string]any{
					"uploadedBytes": s.uploaded, "downloadedBytes": 1, "secondsActive": 1,
				},
				"current-stats": map[string]any{"uploadedBytes": s.uploaded},
			}
		case "torrent-get":
			// 一个 IPv4 peer 和一个 IPv6 peer
			args = map[string]any{
				"torrents": []map[string]any{{
					"id": 1, "name": "t", "rateUpload": s.upSpeed, "uploadedEver": s.uploaded,
					"peers": []map[string]any{
						{"address": "1.2.3.4", "port": 51413, "rateToPeer": s.upSpeed / 2},
						{"address": "2001:db8::1", "port": 51413, "rateToPeer": s.upSpeed / 2},
					},
				}},
			}
		case "session-set":
			var a map[string]any
			_ = json.Unmarshal(req.Arguments, &a)
			if v, ok := a["speed-limit-up"]; ok {
				s.upLimitKB = int64(v.(float64))
			}
			if v, ok := a["speed-limit-up-enabled"]; ok {
				if b, isBool := v.(bool); isBool && !b {
					s.upLimitKB = 0
				}
			}
			args = map[string]any{}
		default:
			_ = json.NewEncoder(w).Encode(map[string]any{"result": "method not found"})
			return
		}

		_ = json.NewEncoder(w).Encode(map[string]any{"result": "success", "arguments": args})
	})
}

func newTRStub() *trStub {
	return &trStub{sessionID: "TEST-SESSION-ID", upSpeed: 2048, uploaded: 1000000}
}

// ---------------------------------------------------------------------------
// 会话协商
// ---------------------------------------------------------------------------

// TestTransmissionSessionNegotiation 验证 409 会话协商能正常完成。
//
// 这是 Transmission 对接中最容易出错的一环：首次请求必然被拒（409），
// 客户端必须从响应头取到 session id 并重发。
func TestTransmissionSessionNegotiation(t *testing.T) {
	stub := newTRStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL, Timeout: 5 * time.Second,
	})

	st, err := c.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}

	if atomic.LoadInt32(&stub.conflicts) == 0 {
		t.Error("应当发生过至少一次 409 会话协商")
	}
	if stub.lastSessionID != stub.sessionID {
		t.Errorf("重发时未带上正确的 session id: %q", stub.lastSessionID)
	}
	if st.TorrentCount != 3 {
		t.Errorf("种子数 = %d, 期望 3", st.TorrentCount)
	}
}

// TestTransmissionRetrySufficient 验证重试次数足以完成会话协商。
//
// call() 用的是 retry(attempts=2)。流程是：
//
//	第 1 次：409（未带 id）
//	第 2 次：带 id 成功
//
// 恰好用满。若将来有人把 attempts 调小，或协商流程多一步，
// 这里会失败并明确暴露出来。
func TestTransmissionRetrySufficient(t *testing.T) {
	stub := newTRStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL, Timeout: 5 * time.Second,
	})

	if err := c.call(context.Background(), "session-get", nil, nil); err != nil {
		t.Fatalf("会话协商应当成功，实际失败: %v", err)
	}
}

// TestTransmissionSessionExpiryRecovers 验证服务端轮换 session id 后能自动恢复。
//
// 长时间运行的场景：Transmission 重启或会话超时后 id 会变，
// 客户端必须能自愈，否则监控会永久中断。
func TestTransmissionSessionExpiryRecovers(t *testing.T) {
	stub := newTRStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL, Timeout: 5 * time.Second,
	})

	// 先成功一次，建立会话
	if _, err := c.Fetch(context.Background()); err != nil {
		t.Fatalf("首次 Fetch 失败: %v", err)
	}

	// 服务端轮换 id，模拟重启
	stub.sessionID = "NEW-SESSION-ID-AFTER-RESTART"

	// 客户端缓存的旧 id 已失效，应当自动重新协商
	st, err := c.Fetch(context.Background())
	if err != nil {
		t.Fatalf("会话轮换后应当能自愈，实际失败: %v", err)
	}
	if st.TorrentCount != 3 {
		t.Errorf("种子数 = %d, 期望 3", st.TorrentCount)
	}
	if stub.lastSessionID != "NEW-SESSION-ID-AFTER-RESTART" {
		t.Errorf("未更新到新的 session id: %q", stub.lastSessionID)
	}
}

// ---------------------------------------------------------------------------
// 认证
// ---------------------------------------------------------------------------

// TestTransmissionBasicAuth 验证 RPC 用户名密码认证。
func TestTransmissionBasicAuth(t *testing.T) {
	stub := newTRStub()
	stub.requireAuth = true
	stub.user, stub.pass = "admin", "secret"
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL,
		Username: "admin", Password: "secret", Timeout: 5 * time.Second,
	})

	if _, err := c.Fetch(context.Background()); err != nil {
		t.Fatalf("带正确凭据应当成功: %v", err)
	}
	if atomic.LoadInt32(&stub.authFailures) != 0 {
		t.Error("不应出现认证失败")
	}
}

// TestTransmissionAuthFailureIsClear 验证密码错误时给出可操作的提示。
func TestTransmissionAuthFailureIsClear(t *testing.T) {
	stub := newTRStub()
	stub.requireAuth = true
	stub.user, stub.pass = "admin", "secret"
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL,
		Username: "admin", Password: "WRONG", Timeout: 5 * time.Second,
	})

	_, err := c.Fetch(context.Background())
	if err == nil {
		t.Fatal("密码错误时应当返回错误")
	}
	if !strings.Contains(err.Error(), "401") && !strings.Contains(err.Error(), "认证") {
		t.Errorf("错误信息应说明是认证问题，实际: %v", err)
	}
}

// ---------------------------------------------------------------------------
// 限速下发
// ---------------------------------------------------------------------------

// TestTransmissionSetUploadLimit 验证限速下发与读回。
func TestTransmissionSetUploadLimit(t *testing.T) {
	stub := newTRStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL, Timeout: 5 * time.Second,
	})

	// 下发 1 MiB/s = 1048576 B/s，应换算为 1049 KB/s（向上取整）
	if err := c.SetUploadLimit(context.Background(), SpeedLimit{
		Total: 1048576,
	}); err != nil {
		t.Fatalf("下发限速失败: %v", err)
	}
	if stub.upLimitKB != 1049 {
		t.Errorf("下发的 KB/s = %d, 期望 1049（1048576 B/s 向上取整）", stub.upLimitKB)
	}

	// 再确认能被读回来（Fetch 会把 KB/s 转回 B/s）
	st, err := c.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if !st.SpeedLimited {
		t.Error("下发限速后 SpeedLimited 应为 true")
	}
	// 1049 KB/s -> 1049000 B/s，略大于原值，属于向上取整的正常结果
	if st.CurrentUpLimit != 1049000 {
		t.Errorf("读回的限速 = %d, 期望 1049000", st.CurrentUpLimit)
	}
}

// TestTransmissionUnlimitedClears 验证下发 0 表示取消限速。
func TestTransmissionUnlimitedClears(t *testing.T) {
	stub := newTRStub()
	stub.upLimitKB = 500
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL, Timeout: 5 * time.Second,
	})

	if err := c.SetUploadLimit(context.Background(), NewUnlimited()); err != nil {
		t.Fatalf("取消限速失败: %v", err)
	}
	if stub.upLimitKB != 0 {
		t.Errorf("取消限速后 KB/s = %d, 期望 0", stub.upLimitKB)
	}
}

// ---------------------------------------------------------------------------
// 数据解析
// ---------------------------------------------------------------------------

// TestTransmissionFetchParsesStats 验证速率与累计量的解析。
func TestTransmissionFetchParsesStats(t *testing.T) {
	stub := newTRStub()
	stub.upSpeed = 4096
	stub.uploaded = 12345678
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := NewTransmission(Options{
		Name: "tr", Type: "transmission", URL: srv.URL, Timeout: 5 * time.Second,
	})

	st, err := c.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if st.TotalUpSpeed() != 4096 {
		t.Errorf("上传速度 = %d, 期望 4096", st.TotalUpSpeed())
	}
	if st.TotalUpBytes() != 12345678 {
		t.Errorf("累计上传 = %d, 期望 12345678", st.TotalUpBytes())
	}
	if st.DlSpeed != 1024 {
		t.Errorf("下载速度 = %d, 期望 1024", st.DlSpeed)
	}
}

// TestTransmissionUnreachableIsClear 验证连不上时给出清晰错误。
func TestTransmissionUnreachableIsClear(t *testing.T) {
	c := NewTransmission(Options{
		Name: "tr", Type: "transmission",
		URL: "http://127.0.0.1:1", Timeout: 2 * time.Second,
	})
	_, err := c.Fetch(context.Background())
	if err == nil {
		t.Fatal("连不上时应当返回错误")
	}
}

// TestTransmissionRPCPathNormalization 验证各种 URL 写法都能拼出正确的 RPC 路径。
func TestTransmissionRPCPathNormalization(t *testing.T) {
	cases := map[string]string{
		"http://host:9091":                   "http://host:9091/transmission/rpc",
		"http://host:9091/":                  "http://host:9091/transmission/rpc",
		"http://host:9091/transmission/rpc":  "http://host:9091/transmission/rpc",
		"http://host:9091/transmission/web":  "http://host:9091/transmission/rpc",
		"http://host:9091/transmission/web/": "http://host:9091/transmission/rpc",
	}
	for in, want := range cases {
		c := NewTransmission(Options{Name: "t", Type: "transmission", URL: in})
		if got := c.rpcPath(); got != want {
			t.Errorf("rpcPath(%q) = %q, 期望 %q", in, got, want)
		}
	}
}
