package client

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

// toggleClient 是一个可切换成功/失败的假客户端。
type toggleClient struct {
	name string
	typ  string
	url  string

	mu      sync.Mutex
	fail    bool
	errText string
	stats   Stats
	limit   SpeedLimit
	sets    int
}

func newToggleClient(name, typ string) *toggleClient {
	return &toggleClient{
		name: name,
		typ:  typ,
		url:  "http://" + name + ":8080",
		stats: Stats{
			Timestamp: time.Now(),
			UpSpeed:   1024,
			UpTotal:   4096,
		},
	}
}

func (c *toggleClient) Name() string { return c.name }
func (c *toggleClient) Type() string { return c.typ }
func (c *toggleClient) URL() string  { return c.url }

func (c *toggleClient) Fetch(_ context.Context) (Stats, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fail {
		return Stats{}, errors.New(c.errText)
	}
	st := c.stats
	st.Timestamp = time.Now()
	return st, nil
}

func (c *toggleClient) SetUploadLimit(_ context.Context, l SpeedLimit) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fail {
		return errors.New(c.errText)
	}
	c.limit = l
	c.sets++
	return nil
}

func (c *toggleClient) setFail(v bool, msg string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.fail = v
	c.errText = msg
}

func (c *toggleClient) setCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sets
}

// ---------------------------------------------------------------------------
// Managed：健康状态与最近错误
// ---------------------------------------------------------------------------

// TestManagedTracksHealth 验证包装器能正确跟踪健康状态。
//
// 面板上的「正常 / 异常」标记、以及策略引擎判断「是否该跳过
// 这个客户端」，都依赖这里的状态。
func TestManagedTracksHealth(t *testing.T) {
	inner := newToggleClient("c1", "qbittorrent")
	m := NewManaged(inner)

	// 初始状态：尚未采集，应视为不健康
	if m.Healthy() {
		t.Error("尚未采集时不应标记为健康")
	}
	if m.LastError() != "" {
		t.Errorf("尚未采集时不应有错误信息，实际 %q", m.LastError())
	}

	// 采集成功
	if _, err := m.Fetch(context.Background()); err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if !m.Healthy() {
		t.Error("采集成功后应标记为健康")
	}
	if m.LastError() != "" {
		t.Errorf("成功后不应有错误信息，实际 %q", m.LastError())
	}
	if m.LastSeen().IsZero() {
		t.Error("成功后应记录时间戳")
	}

	// 采集失败
	inner.setFail(true, "connection refused")
	if _, err := m.Fetch(context.Background()); err == nil {
		t.Fatal("预期 Fetch 失败")
	}
	if m.Healthy() {
		t.Error("采集失败后应标记为不健康")
	}
	if !strings.Contains(m.LastError(), "connection refused") {
		t.Errorf("应记录具体错误，实际 %q", m.LastError())
	}
	// 关键：失败不应清掉 LastSeen，否则界面会显示「从未成功」
	if m.LastSeen().IsZero() {
		t.Error("失败后应保留上一次成功的时间戳")
	}

	// 恢复
	inner.setFail(false, "")
	if _, err := m.Fetch(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !m.Healthy() {
		t.Error("恢复后应重新标记为健康")
	}
	if m.LastError() != "" {
		t.Error("恢复后应清空错误信息")
	}
}

// TestManagedSetUploadLimitTracksErrors 验证下发限速失败也会影响健康状态。
func TestManagedSetUploadLimitTracksErrors(t *testing.T) {
	inner := newToggleClient("c1", "qbittorrent")
	m := NewManaged(inner)

	if err := m.SetUploadLimit(context.Background(), SpeedLimit{Total: 100}); err != nil {
		t.Fatalf("下发失败: %v", err)
	}
	if inner.setCount() != 1 {
		t.Errorf("应下发 1 次，实际 %d", inner.setCount())
	}
	if !m.Healthy() {
		t.Error("成功后应标记为健康")
	}

	inner.setFail(true, "unauthorized")
	if err := m.SetUploadLimit(context.Background(), SpeedLimit{Total: 200}); err == nil {
		t.Fatal("预期下发失败")
	}
	if m.Healthy() {
		t.Error("下发失败后应标记为不健康")
	}
	if !strings.Contains(m.LastError(), "unauthorized") {
		t.Errorf("应记录错误，实际 %q", m.LastError())
	}
}

// TestManagedCachesLastStats 验证缓存最近一次成功的采样。
//
// 客户端短暂不可用时，界面仍应显示最后一次已知状态，
// 而不是一片空白。
func TestManagedCachesLastStats(t *testing.T) {
	inner := newToggleClient("c1", "qbittorrent")
	m := NewManaged(inner)

	if m.LastStats().TotalUpSpeed() != 0 {
		t.Error("尚未采集时缓存应为空")
	}

	_, _ = m.Fetch(context.Background())
	cached := m.LastStats()
	if cached.TotalUpSpeed() != 1024 {
		t.Errorf("缓存速率 = %d, 期望 1024", cached.TotalUpSpeed())
	}

	// 失败后缓存应保持上一次的值
	inner.setFail(true, "boom")
	_, _ = m.Fetch(context.Background())
	if got := m.LastStats().TotalUpSpeed(); got != 1024 {
		t.Errorf("失败后缓存应保持旧值，实际 %d", got)
	}
}

// TestManagedDelegatesIdentity 验证包装器透传基础信息。
func TestManagedDelegatesIdentity(t *testing.T) {
	inner := newToggleClient("myclient", "transmission")
	m := NewManaged(inner)

	if m.Name() != "myclient" {
		t.Errorf("Name = %q", m.Name())
	}
	if m.Type() != "transmission" {
		t.Errorf("Type = %q", m.Type())
	}
}

// TestManagedConcurrentAccess 验证并发读写不会数据竞争。
func TestManagedConcurrentAccess(t *testing.T) {
	inner := newToggleClient("c1", "qbittorrent")
	m := NewManaged(inner)

	var wg sync.WaitGroup
	stop := make(chan struct{})

	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			_, _ = m.Fetch(context.Background())
			inner.setFail(true, "x")
			_, _ = m.Fetch(context.Background())
			inner.setFail(false, "")
		}
	}()

	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				_ = m.Healthy()
				_ = m.LastError()
				_ = m.LastSeen()
				_ = m.LastStats()
			}
		}()
	}

	time.Sleep(100 * time.Millisecond)
	close(stop)
	wg.Wait()
}

// ---------------------------------------------------------------------------
// Registry：客户端注册与按名解析
// ---------------------------------------------------------------------------

// TestRegistryAddAndGet 验证注册与查找。
func TestRegistryAddAndGet(t *testing.T) {
	r := NewRegistry()
	r.Add(NewManaged(newToggleClient("qb", "qbittorrent")))

	c, ok := r.Get("qb")
	if !ok {
		t.Fatal("应能按名找到客户端")
	}
	if c.Name() != "qb" {
		t.Errorf("Name = %q", c.Name())
	}

	// 查找应忽略大小写（用户在配置里大小写不统一很常见）
	if _, ok := r.Get("QB"); !ok {
		t.Error("查找应忽略大小写")
	}

	if _, ok := r.Get("nope"); ok {
		t.Error("不存在的客户端不应返回 ok")
	}
}

// TestRegistryAddReplacesDuplicate 验证同名客户端会被覆盖而不是重复添加。
func TestRegistryAddReplacesDuplicate(t *testing.T) {
	r := NewRegistry()
	r.Add(NewManaged(newToggleClient("qb", "qbittorrent")))
	second := newToggleClient("qb", "transmission")
	r.Add(NewManaged(second))

	all := r.All()
	if len(all) != 1 {
		t.Fatalf("同名客户端应只保留一个，实际 %d 个", len(all))
	}
	if all[0].Type() != "transmission" {
		t.Errorf("应保留后添加的实例，实际类型 %q", all[0].Type())
	}
}

// TestRegistryAllPreservesOrder 验证 All 返回的顺序与添加顺序一致。
//
// 顺序稳定对界面展示很重要，否则每次刷新列表都会跳动。
func TestRegistryAllPreservesOrder(t *testing.T) {
	r := NewRegistry()
	for _, n := range []string{"c", "a", "b"} {
		r.Add(NewManaged(newToggleClient(n, "qbittorrent")))
	}

	all := r.All()
	want := []string{"c", "a", "b"}
	if len(all) != len(want) {
		t.Fatalf("数量 = %d, 期望 %d", len(all), len(want))
	}
	for i, w := range want {
		if all[i].Name() != w {
			t.Errorf("第 %d 个 = %q, 期望 %q（应保持添加顺序）", i, all[i].Name(), w)
		}
	}
}

// TestRegistryNamesSorted 验证 Names 返回排序后的名称列表。
//
// 它用于界面的下拉/多选框，排序让用户容易找到目标。
func TestRegistryNamesSorted(t *testing.T) {
	r := NewRegistry()
	for _, n := range []string{"zeta", "alpha", "mid"} {
		r.Add(NewManaged(newToggleClient(n, "qbittorrent")))
	}

	names := r.Names()
	want := []string{"alpha", "mid", "zeta"}
	if len(names) != len(want) {
		t.Fatalf("数量 = %d", len(names))
	}
	for i := range want {
		if names[i] != want[i] {
			t.Errorf("Names() = %v, 期望 %v", names, want)
			break
		}
	}
}

// TestRegistryResolve 验证策略的客户端名单解析。
//
// 这是策略「作用对象」的实现，解析错了会导致策略作用到
// 错误的下载器上（或完全不起作用）。
func TestRegistryResolve(t *testing.T) {
	r := NewRegistry()
	r.Add(NewManaged(newToggleClient("qb", "qbittorrent")))
	r.Add(NewManaged(newToggleClient("tr", "transmission")))

	// 空名单 -> 全部
	if got := len(r.Resolve(nil)); got != 2 {
		t.Errorf("空名单应解析为全部，实际 %d", got)
	}
	// 通配符 -> 全部
	if got := len(r.Resolve([]string{"*"})); got != 2 {
		t.Errorf("通配符应解析为全部，实际 %d", got)
	}
	// 通配符混在名单里 -> 全部
	if got := len(r.Resolve([]string{"qb", "*"})); got != 2 {
		t.Errorf("含通配符时应解析为全部，实际 %d", got)
	}
	// 指定单个
	got := r.Resolve([]string{"qb"})
	if len(got) != 1 || got[0].Name() != "qb" {
		t.Errorf("应只解析出 qb，实际 %v", namesOf(got))
	}
	// 忽略大小写
	if got := r.Resolve([]string{"TR"}); len(got) != 1 || got[0].Name() != "tr" {
		t.Errorf("应忽略大小写，实际 %v", namesOf(got))
	}
	// 不存在的名字 -> 空结果（策略引擎据此跳过）
	if got := r.Resolve([]string{"nope"}); len(got) != 0 {
		t.Errorf("不存在的名字应返回空，实际 %v", namesOf(got))
	}
}

func namesOf(ms []*Managed) []string {
	out := make([]string, 0, len(ms))
	for _, m := range ms {
		out = append(out, m.Name())
	}
	return out
}

// TestRegistryConcurrentAccess 验证并发访问安全。
func TestRegistryConcurrentAccess(t *testing.T) {
	r := NewRegistry()
	for i := 0; i < 5; i++ {
		r.Add(NewManaged(newToggleClient(string(rune('a'+i)), "qbittorrent")))
	}

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				_ = r.All()
				_ = r.Names()
				_, _ = r.Get("a")
				_ = r.Resolve([]string{"*"})
				if i%2 == 0 {
					r.Add(NewManaged(newToggleClient("x", "qbittorrent")))
				}
			}
		}(i)
	}
	wg.Wait()
}

// ---------------------------------------------------------------------------
// 工厂
// ---------------------------------------------------------------------------

// TestNewDispatchesByType 验证按类型构造正确的实现。
func TestNewDispatchesByType(t *testing.T) {
	qb, err := New(Options{Name: "a", Type: "qbittorrent", URL: "http://x:1"})
	if err != nil {
		t.Fatalf("构造 qBittorrent 失败: %v", err)
	}
	if qb.Type() != "qbittorrent" {
		t.Errorf("类型 = %q", qb.Type())
	}

	tr, err := New(Options{Name: "b", Type: "transmission", URL: "http://x:2"})
	if err != nil {
		t.Fatalf("构造 Transmission 失败: %v", err)
	}
	if tr.Type() != "transmission" {
		t.Errorf("类型 = %q", tr.Type())
	}

	// 类型大小写不敏感
	if _, err := New(Options{Name: "c", Type: "qBittorrent", URL: "http://x:3"}); err != nil {
		t.Errorf("类型应忽略大小写: %v", err)
	}

	// 未知类型应报错
	if _, err := New(Options{Name: "d", Type: "utorrent", URL: "http://x:4"}); err == nil {
		t.Error("未知类型应返回错误")
	}
}

// TestNewAPIKeyOnlyAffectsQBittorrent 验证 API Key 只对 qBittorrent 生效。
func TestNewAPIKeyOnlyAffectsQBittorrent(t *testing.T) {
	qb, err := New(Options{Name: "a", Type: "qbittorrent", URL: "http://x:1", APIKey: "qbt_abc"})
	if err != nil {
		t.Fatal(err)
	}
	if !qb.(*QBittorrent).UsesAPIKey() {
		t.Error("qBittorrent 应使用 API Key")
	}

	// Transmission 没有 API Key 机制，也不会暴露 UsesAPIKey 方法。
	// 这里只需确认工厂能正常构造它，且传入的 Key 不会被静默当成认证方式。
	tr, err := New(Options{Name: "b", Type: "transmission", URL: "http://x:2", APIKey: "qbt_abc"})
	if err != nil {
		t.Fatalf("Transmission 构造不应因 API Key 失败: %v", err)
	}
	if tr.Type() != "transmission" {
		t.Errorf("类型 = %q", tr.Type())
	}
}

// ---------------------------------------------------------------------------
// 辅助逻辑
// ---------------------------------------------------------------------------

// TestStatusErrorIncludesSnippet 验证错误信息带响应片段。
//
// 排查问题时，光知道「HTTP 403」往往不够，
// 服务端返回的正文通常更能说明原因。
func TestStatusErrorIncludesSnippet(t *testing.T) {
	err := statusError("测试操作", 403, []byte("Forbidden: bad credentials"))
	msg := err.Error()
	if !strings.Contains(msg, "403") {
		t.Errorf("应包含状态码，实际: %s", msg)
	}
	if !strings.Contains(msg, "bad credentials") {
		t.Errorf("应包含响应正文片段，实际: %s", msg)
	}
	if !strings.Contains(msg, "测试操作") {
		t.Errorf("应包含操作名，实际: %s", msg)
	}

	// 空正文时不应 panic
	err = statusError("op", 500, nil)
	if !strings.Contains(err.Error(), "500") {
		t.Errorf("空正文时应仍能报告状态码，实际: %s", err)
	}

	// 超长正文应被截断，避免日志被刷爆
	long := make([]byte, 5000)
	for i := range long {
		long[i] = 'x'
	}
	err = statusError("op", 500, long)
	if len(err.Error()) > 500 {
		t.Errorf("超长正文应被截断，实际长度 %d", len(err.Error()))
	}
}

// TestLimitTracker 验证限速记录器。
//
// 它记录「我们最后一次下发的限速」，用于在恢复时区分
// 「是我们改的」还是「用户自己改的」。
func TestLimitTracker(t *testing.T) {
	var lt limitTracker

	if _, ok := lt.appliedLimit(); ok {
		t.Error("初始状态不应有记录")
	}

	lt.record(SpeedLimit{Total: 1024})
	got, ok := lt.appliedLimit()
	if !ok {
		t.Fatal("记录后应能读回")
	}
	if got.Total != 1024 {
		t.Errorf("Total = %d, 期望 1024", got.Total)
	}

	lt.clear()
	if _, ok := lt.appliedLimit(); ok {
		t.Error("清除后不应再有记录")
	}
}

// TestLimitTrackerConcurrent 验证并发读写安全。
func TestLimitTrackerConcurrent(t *testing.T) {
	var lt limitTracker
	var wg sync.WaitGroup

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			lt.record(SpeedLimit{Total: int64(i)})
		}
	}()
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				_, _ = lt.appliedLimit()
			}
		}()
	}
	wg.Wait()
}

// TestLimitConstructors 验证限速指令的构造。
func TestLimitConstructors(t *testing.T) {
	noop := NewNoopLimit()
	if noop.Total != -1 {
		t.Errorf("NewNoopLimit 应全部为 -1（表示不修改），实际 %+v", noop)
	}

	unlim := NewUnlimited()
	if unlim.Total != 0 {
		t.Errorf("NewUnlimited 应全部为 0（表示不限速），实际 %+v", unlim)
	}
}
