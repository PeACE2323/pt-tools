package web

import (
	"fmt"
	"strings"

	"github.com/btguard/bt-traffic-guard/internal/config"
	"gopkg.in/yaml.v3"
)

// marshalYAML 把配置序列化为 YAML 文本。
func marshalYAML(cfg *config.Config) ([]byte, error) {
	out, err := yaml.Marshal(cfg)
	if err != nil {
		return nil, apiError{Code: 500, Message: "序列化配置失败: " + err.Error()}
	}
	return out, nil
}

// parseYAML 解析 YAML 文本为配置，并执行完整校验。
func parseYAML(text string) (*config.Config, error) {
	// 去掉可能存在的 UTF-8 BOM（用户从记事本复制粘贴时常见）。
	text = strings.TrimPrefix(text, "\ufeff")

	cfg := config.Default()
	dec := yaml.NewDecoder(strings.NewReader(text))
	// 严格模式：拒绝未知字段，避免用户拼错键名却不生效。
	dec.KnownFields(true)
	if err := dec.Decode(cfg); err != nil {
		return nil, fmt.Errorf("YAML 解析失败: %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// sanitizeConfig 返回一份【已擦除敏感字段】的配置副本，用于经由 HTTP 暴露给前端。
//
// 为什么必须擦除：面板的访问认证默认是关闭的。
// 若直接回传原始配置，任何能访问该端口的人都能读到
// 下载器密码、qBittorrent API Key、ntfy 令牌与面板密码本身。
//
// 擦除方式是用固定的占位符替换（而不是清空），
// 这样前端能知道「该字段已配置」，用户不改动时服务端会沿用原值。
//
// 注意：返回的是深拷贝，绝不修改传入的 cfg，
// 否则会把运行中的配置真的改掉。
func sanitizeConfig(cfg *config.Config, mask bool) *config.Config {
	if cfg == nil {
		return nil
	}
	if !mask {
		// 仅在服务端内部使用（不经过 HTTP）时才允许拿原始配置。
		// 任何面向 HTTP 的调用都必须传 mask=true。
		return cfg
	}

	out := *cfg

	// 客户端：擦除密码与 API Key
	out.Clients = make([]config.ClientConfig, len(cfg.Clients))
	copy(out.Clients, cfg.Clients)
	for i := range out.Clients {
		out.Clients[i].Password = maskSecret(cfg.Clients[i].Password)
		out.Clients[i].APIKey = maskSecret(cfg.Clients[i].APIKey)
	}

	// 面板认证密码
	out.WebAuth = cfg.WebAuth
	out.WebAuth.Password = maskSecret(cfg.WebAuth.Password)

	// 通知：ntfy 令牌与 Webhook 自定义头（后者常带 Authorization）
	out.Notify = cfg.Notify
	out.Notify.Ntfy.Token = maskSecret(cfg.Notify.Ntfy.Token)
	if len(cfg.Notify.Webhook.Headers) > 0 {
		headers := make(map[string]string, len(cfg.Notify.Webhook.Headers))
		for k, v := range cfg.Notify.Webhook.Headers {
			if isSensitiveHeader(k) {
				headers[k] = maskSecret(v)
			} else {
				headers[k] = v
			}
		}
		out.Notify.Webhook.Headers = headers
	}

	return &out
}

// maskSecret 把非空秘密替换为占位符；空值保持为空。
func maskSecret(v string) string {
	if strings.TrimSpace(v) == "" {
		return ""
	}
	return passwordMask
}

// isSensitiveHeader 判断某个自定义请求头是否可能携带凭据。
func isSensitiveHeader(name string) bool {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "authorization", "x-api-key", "api-key", "x-auth-token",
		"cookie", "proxy-authorization", "x-token":
		return true
	}
	return false
}
