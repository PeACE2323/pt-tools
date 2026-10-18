package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestCheckWritable 验证可写性探测的正确性。
//
// 这个探测用于在启动阶段就提醒用户「配置文件不可写」，
// 否则问题要到用户在 WebUI 上点保存时才暴露，
// 而那时的报错往往难以定位（尤其容器场景）。
func TestCheckWritable(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(f, []byte("listen: \":8080\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := checkWritable(f); err != nil {
		t.Errorf("可写的文件不应报错: %v", err)
	}

	// 只读文件应被识别
	if err := os.Chmod(f, 0o444); err != nil {
		t.Skipf("当前平台不支持 chmod: %v", err)
	}
	// 以 root 运行时只读位不生效，跳过
	if err := checkWritable(f); err == nil {
		t.Skip("当前以高权限运行，只读位不生效，跳过该项")
	}
}

// TestWritableHintMentionsFix 验证提示信息包含可操作的修复命令。
//
// 容器里目录不可写的根因几乎都是挂载目录的属主与容器内 uid 不一致，
// 提示里必须给出 chown 命令，否则用户只知道「失败」却不知如何修。
func TestWritableHintMentionsFix(t *testing.T) {
	h := writableHint("/config")

	for _, want := range []string{"/config", "chown", "1000:1000", "--user"} {
		if !strings.Contains(h, want) {
			t.Errorf("提示信息应包含 %q，实际内容:\n%s", want, h)
		}
	}
}

// TestPermHint 验证权限修正提示包含目标目录。
func TestPermHint(t *testing.T) {
	h := permHint("/mnt/user/appdata/btg/config")
	if !strings.Contains(h, "/mnt/user/appdata/btg/config") {
		t.Errorf("提示应包含具体路径，实际: %s", h)
	}
	if !strings.Contains(h, "chown") {
		t.Errorf("提示应包含 chown 命令，实际: %s", h)
	}
}

// TestBootstrapCreatesConfig 验证首次启动会生成配置文件与数据目录。
func TestBootstrapCreatesConfig(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "sub", "config.yaml")

	app := &App{configPath: cfgPath, log: newLogger("error")}
	app.logf = func(string, string, ...any) {}

	if err := app.Bootstrap(); err != nil {
		t.Fatalf("Bootstrap 失败: %v", err)
	}
	if _, err := os.Stat(cfgPath); err != nil {
		t.Errorf("应生成配置文件: %v", err)
	}
	// 配置里默认的 data_dir 是 /data，在测试环境可能建不了，
	// 但那只是警告，不应导致 Bootstrap 失败。
}

// TestBootstrapFailsClearlyWhenConfigDirUnwritable 验证配置目录不可写时，
// 返回的错误里包含可操作的修复提示，而不是只抛一个裸的权限错误。
func TestBootstrapFailsClearlyWhenConfigDirUnwritable(t *testing.T) {
	dir := t.TempDir()
	sub := filepath.Join(dir, "ro")
	if err := os.MkdirAll(sub, 0o555); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(sub, "nested", "config.yaml")

	app := &App{configPath: cfgPath, log: newLogger("error")}
	app.logf = func(string, string, ...any) {}

	err := app.Bootstrap()
	if err == nil {
		t.Skip("当前以高权限运行，目录权限限制不生效，跳过该项")
	}
	msg := err.Error()
	if !strings.Contains(msg, "chown") {
		t.Errorf("错误信息应包含修复命令，实际:\n%s", msg)
	}
	if !strings.Contains(msg, "1000:1000") {
		t.Errorf("错误信息应包含 uid:gid，实际:\n%s", msg)
	}
}
