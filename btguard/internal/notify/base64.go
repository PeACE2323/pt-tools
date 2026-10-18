package notify

import "encoding/base64"

// base64Std 是 encoding/base64 的薄封装，便于测试替换。
func base64Std(s string) string {
	return base64.StdEncoding.EncodeToString([]byte(s))
}
