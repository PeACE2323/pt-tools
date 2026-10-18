package web

import (
	"encoding/json"
	"net/http"
	"testing"
)

// TestBareNumberAcceptedInForm 验证「只填数字」能被正确解析。
//
// 界面上数值字段拆成了「数字输入 + 单位下拉」，
// 用户只填数字时单位可能是空串（字节 / B/s）。
// 后端必须原样接受，并按对应语义换算。
func TestBareNumberAcceptedInForm(t *testing.T) {
	f := newFullServer(t, nil)

	w := f.do(t, http.MethodGet, "/api/config/form", "", nil)
	var view formConfig
	if err := json.Unmarshal(w.Body.Bytes(), &view); err != nil {
		t.Fatal(err)
	}
	p := &view.Policies[0]

	// 纯数字：单位留空
	p.ThresholdTotal = "500" // 500 字节
	p.LimitTotal = "1024"    // 1024 B/s

	body, _ := json.Marshal(view)
	w = f.do(t, http.MethodPost, "/api/config/form", string(body), nil)
	if w.Code != 200 {
		t.Fatalf("保存纯数字失败: %d %s", w.Code, w.Body.String())
	}
	got := f.cfg.Policies[0]
	if got.ThTotal() != 500 {
		t.Errorf("阈值 = %d, 期望 500（纯数字按字节解释）", got.ThTotal())
	}
	if got.LimTotal() != 1024 {
		t.Errorf("限速 = %d, 期望 1024（纯数字按 B/s 解释）", got.LimTotal())
	}
}

// TestNumberWithUnitAcceptedInForm 验证「数字+单位」组合。
func TestNumberWithUnitAcceptedInForm(t *testing.T) {
	f := newFullServer(t, nil)

	w := f.do(t, http.MethodGet, "/api/config/form", "", nil)
	var view formConfig
	_ = json.Unmarshal(w.Body.Bytes(), &view)
	p := &view.Policies[0]

	// 模拟前端把「数字 + 单位下拉」拼起来的结果
	p.ThresholdTotal = "500GiB"
	p.LimitTotal = "5MiB"
	p.ResumeLimitTotal = "20MiB"

	body, _ := json.Marshal(view)
	w = f.do(t, http.MethodPost, "/api/config/form", string(body), nil)
	if w.Code != 200 {
		t.Fatalf("保存失败: %d %s", w.Code, w.Body.String())
	}

	got := f.cfg.Policies[0]
	if got.ThTotal() != 500<<30 {
		t.Errorf("阈值 = %d, 期望 %d", got.ThTotal(), int64(500)<<30)
	}
	if got.LimTotal() != 5<<20 {
		t.Errorf("限速 = %d, 期望 %d", got.LimTotal(), int64(5)<<20)
	}
	if got.ResumeTotal() != 20<<20 {
		t.Errorf("恢复速度 = %d, 期望 %d", got.ResumeTotal(), int64(20)<<20)
	}
}
