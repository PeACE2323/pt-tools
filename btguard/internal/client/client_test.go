package client

import "testing"

func TestCoerceInt64(t *testing.T) {
	cases := []struct {
		in   any
		want int64
	}{
		{nil, 0},
		{float64(42), 42},
		{int(7), 7},
		{int64(99), 99},
		{"123", 123},
		{true, 1},
		{false, 0},
		{"abc", 0},
	}
	for _, c := range cases {
		if got := coerceInt64(c.in); got != c.want {
			t.Errorf("coerceInt64(%v) = %d, 期望 %d", c.in, got, c.want)
		}
	}
}

func TestCoerceBool(t *testing.T) {
	cases := []struct {
		in   any
		want bool
	}{
		{nil, false},
		{true, true},
		{false, false},
		{float64(1), true},
		{float64(0), false},
		{"true", true},
		{"yes", true},
		{"no", false},
	}
	for _, c := range cases {
		if got := coerceBool(c.in); got != c.want {
			t.Errorf("coerceBool(%v) = %v, 期望 %v", c.in, got, c.want)
		}
	}
}

// TestVersionAtLeast 验证 qBittorrent 版本比较（用于 API Key 版本提示）。
func TestVersionAtLeast(t *testing.T) {
	cases := []struct {
		v    string
		maj  int
		min  int
		pat  int
		want bool
	}{
		{"4.6.0", 4, 6, 0, true},
		{"4.6.5", 4, 6, 0, true},
		{"4.6.0-beta1", 4, 6, 0, true},
		{"4.5.5", 4, 6, 0, false},
		{"5.0.0", 4, 6, 0, true},
		{"4.6", 4, 6, 0, true},
		{"4", 4, 6, 0, false},
		{"v4.6.2", 4, 6, 0, false}, // 未去 v 前缀时按 0 处理
		{"", 4, 6, 0, false},
	}
	for _, c := range cases {
		if got := versionAtLeast(c.v, c.maj, c.min, c.pat); got != c.want {
			t.Errorf("versionAtLeast(%q, %d, %d, %d) = %v, 期望 %v",
				c.v, c.maj, c.min, c.pat, got, c.want)
		}
	}
}

func TestSpeedLimitHelpers(t *testing.T) {
	if l := NewNoopLimit(); l.Total != -1 {
		t.Errorf("NewNoopLimit().Total = %d, 期望 -1", l.Total)
	}
	if l := NewUnlimited(); l.Total != 0 {
		t.Errorf("NewUnlimited().Total = %d, 期望 0", l.Total)
	}
}

func TestStatsTotals(t *testing.T) {
	s := Stats{
		UpSpeed: 300,
		UpTotal: 3000,
	}
	if got := s.TotalUpSpeed(); got != 300 {
		t.Errorf("TotalUpSpeed() = %d, 期望 300", got)
	}
	if got := s.TotalUpBytes(); got != 3000 {
		t.Errorf("TotalUpBytes() = %d, 期望 3000", got)
	}
}
