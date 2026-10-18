package traffic

import (
	"context"
	"testing"
	"time"

	"github.com/btguard/bt-traffic-guard/internal/client"
)

func TestIntegrate(t *testing.T) {
	// 梯形法：平均速率 × 时间
	cases := []struct {
		prev, cur int64
		dt        time.Duration
		want      int64
	}{
		{100, 100, time.Second, 100},
		{0, 200, time.Second, 100},       // 平均 100
		{200, 0, time.Second, 100},       // 平均 100
		{100, 200, 2 * time.Second, 300}, // 平均 150 × 2
		{0, 0, time.Second, 0},
		{100, 100, 0, 0}, // dt <= 0
	}
	for _, c := range cases {
		if got := integrate(c.prev, c.cur, c.dt); got != c.want {
			t.Errorf("integrate(%d, %d, %v) = %d, 期望 %d", c.prev, c.cur, c.dt, got, c.want)
		}
	}
}

func TestCounterReset(t *testing.T) {
	c := &counter{windowBytes: 5000, totalBytes: 9000}
	c.reset()
	if c.windowBytes != 0 {
		t.Errorf("reset 后 windowBytes = %d, 期望 0", c.windowBytes)
	}
	// totalBytes 是进程累计，不应被周期重置影响
	if c.totalBytes != 9000 {
		t.Errorf("reset 后 totalBytes = %d, 期望保持 9000", c.totalBytes)
	}
}

func TestFormatBytes(t *testing.T) {
	cases := []struct {
		in   int64
		want string
	}{
		{0, "0 B"},
		{512, "512 B"},
		{1024, "1.00 KiB"},
		{1536, "1.50 KiB"},
		{1 << 20, "1.00 MiB"},
		{500 * (1 << 30), "500.00 GiB"},
	}
	for _, c := range cases {
		if got := FormatBytes(c.in); got != c.want {
			t.Errorf("FormatBytes(%d) = %q, 期望 %q", c.in, got, c.want)
		}
	}
}

func TestFormatSpeed(t *testing.T) {
	if got := FormatSpeed(0); got != "不限速" {
		t.Errorf("FormatSpeed(0) = %q, 期望 \"不限速\"", got)
	}
	if got := FormatSpeed(-1); got != "不限速" {
		t.Errorf("FormatSpeed(-1) = %q, 期望 \"不限速\"", got)
	}
	if got := FormatSpeed(1 << 20); got != "1.00 MiB/s" {
		t.Errorf("FormatSpeed(1MiB) = %q", got)
	}
}

// TestSnapshotFields 验证快照的字段语义（现在只有单一总量）。
func TestSnapshotFields(t *testing.T) {
	s := ClientSnapshot{
		UpSpeed:     30,
		WindowBytes: 350,
	}
	if s.UpSpeed != 30 {
		t.Errorf("UpSpeed = %d, 期望 30", s.UpSpeed)
	}
	if s.WindowBytes != 350 {
		t.Errorf("WindowBytes = %d, 期望 350", s.WindowBytes)
	}
}

// TestRegistryResolve 验证策略的客户端名单解析（含通配符）。
func TestRegistryResolve(t *testing.T) {
	reg := client.NewRegistry()
	reg.Add(client.NewManaged(&fakeClient{name: "qb"}))
	reg.Add(client.NewManaged(&fakeClient{name: "tr"}))

	if got := len(reg.Resolve(nil)); got != 2 {
		t.Errorf("Resolve(nil) 返回 %d 个, 期望 2", got)
	}
	if got := len(reg.Resolve([]string{"*"})); got != 2 {
		t.Errorf("Resolve([*]) 返回 %d 个, 期望 2", got)
	}
	if got := len(reg.Resolve([]string{"qb"})); got != 1 {
		t.Errorf("Resolve([qb]) 返回 %d 个, 期望 1", got)
	}
	if got := len(reg.Resolve([]string{"QB"})); got != 1 {
		t.Errorf("Resolve 应当忽略大小写, 返回 %d 个", got)
	}
	if got := len(reg.Resolve([]string{"nope"})); got != 0 {
		t.Errorf("Resolve([nope]) 返回 %d 个, 期望 0", got)
	}
}

// fakeClient 是用于测试的最小 Client 实现。
type fakeClient struct {
	name string
}

func (f *fakeClient) Name() string { return f.name }
func (f *fakeClient) Type() string { return "fake" }

func (f *fakeClient) Fetch(_ context.Context) (client.Stats, error) {
	return client.Stats{Timestamp: time.Now()}, nil
}

func (f *fakeClient) SetUploadLimit(_ context.Context, _ client.SpeedLimit) error { return nil }
