package web

import (
	"encoding/json"
	"net/http"
	"testing"
)

// TestResumeLimitFormRoundTrip 验证「解除限速后的速度」经表单存取一致。
func TestResumeLimitFormRoundTrip(t *testing.T) {
	f := newFullServer(t, nil)

	// 读取当前表单
	w := f.do(t, http.MethodGet, "/api/config/form", "", nil)
	if w.Code != 200 {
		t.Fatalf("读取表单失败: %d", w.Code)
	}
	var view formConfig
	if err := json.Unmarshal(w.Body.Bytes(), &view); err != nil {
		t.Fatalf("解析表单: %v", err)
	}
	if len(view.Policies) != 1 {
		t.Fatalf("应有 1 条策略，实际 %d", len(view.Policies))
	}

	// 填写恢复速度
	p := &view.Policies[0]
	p.ResumeLimitTotal = "5MiB"

	body, _ := json.Marshal(view)
	w = f.do(t, http.MethodPost, "/api/config/form", string(body), nil)
	if w.Code != 200 {
		t.Fatalf("保存失败: %d %s", w.Code, w.Body.String())
	}

	// 回读应保持一致
	w = f.do(t, http.MethodGet, "/api/config/form", "", nil)
	_ = json.Unmarshal(w.Body.Bytes(), &view)
	got := view.Policies[0]
	if got.ResumeLimitTotal == "" {
		t.Error("恢复速度未持久化")
	}
	if f.cfg.Policies[0].ResumeTotal() != 5<<20 {
		t.Errorf("ResumeTotal = %d, 期望 %d", f.cfg.Policies[0].ResumeTotal(), 5<<20)
	}
	if !f.cfg.Policies[0].HasResumeLimit() {
		t.Error("HasResumeLimit 应为 true")
	}
}

// TestResumeLimitFormRejectsBadValue 验证非法值被拒绝。
func TestResumeLimitFormRejectsBadValue(t *testing.T) {
	f := newFullServer(t, nil)

	w := f.do(t, http.MethodGet, "/api/config/form", "", nil)
	var view formConfig
	_ = json.Unmarshal(w.Body.Bytes(), &view)
	view.Policies[0].ResumeLimitTotal = "不是速度"

	body, _ := json.Marshal(view)
	w = f.do(t, http.MethodPost, "/api/config/form", string(body), nil)
	if w.Code == 200 {
		t.Error("非法的恢复速度应被拒绝")
	}
}
