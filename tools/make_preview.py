#!/usr/bin/env python3
"""生成一个带 mock 数据的预览页，用于本地渲染验证。

真实页面的设置页需要后端 API 才能渲染，这里拦截 fetch 返回固定数据，
把默认标签页设为「系统设置」，方便用无头浏览器截图检查布局与溢出问题。

注意：这个文件只用于开发期视觉验证，不会打包进镜像（.gitignore 已排除）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "static" / "index.html"
OUT = ROOT / "static" / "_preview.html"

NOW = "Date.now()/1000"

MOCK_SCRIPT = """
<script>
// ---- 预览用 mock：让设置页无需后端即可渲染 ----
window.__MOCK__ = true;
(function(){
  const _origFetch = window.fetch;
  const now = Date.now()/1000;
  const MOCK = {
    '/api/version': {name:'CD2-115-Guard', version:'1.0.0-preview'},
    '/api/dashboard': {
      cd2:{host:'192.168.6.2:19798', connected:true, authenticated:true,
           user_name:'user@example.com', upload_count:3, last_poll_at:now,
           last_poll_ok:true, last_poll_error:''},
      p115:{configured:true, logged_in:true, user_id:1234567, task_count:5,
            last_poll_at:now, last_poll_ok:true, last_poll_error:''},
      stats:{today_instant:12, total_instant:88, live_uploads:3,
             waiting_retry:2, retrying:1, retry_success:40, retry_failed:5,
             cancelled_retry:0, today_cancelled_offline:1,
             total_cancelled_offline:9, enabled_rules:2, total_rules:2,
             offline_active:5},
      scheduler:{running:true, jobs:[]}
    },
    '/api/settings': {
      cd2_host:'192.168.6.2:19798',
      cd2_token_masked:'***************************************a147',
      cd2_token_set:true,
      cd2_username:'',
      cd2_password_set:false,
      cd2_poll_interval:3,
      // 故意给一个很长的脱敏串，用于验证是否会撑破布局
      p115_cookie_masked:'********************************************************************************da7bbdcb',
      p115_cookie_set:true,
      p115_poll_interval:300,
      p115_timeout_threshold:604800,
      p115_auto_cancel:true,
      p115_task_page_size:30,
      webhook_token_set:false,
      webhook_url_hint:''
    },
    '/api/settings/events': [],
    '/api/p115/config': {poll_interval:300, timeout_threshold:604800,
                         auto_cancel:true, task_page_size:30, cookie_set:true}
  };
  window.fetch = function(url){
    const key = String(url).split('?')[0];
    if (Object.prototype.hasOwnProperty.call(MOCK, key)) {
      return Promise.resolve({
        ok:true, status:200,
        text: function(){ return Promise.resolve(JSON.stringify(MOCK[key])); }
      });
    }
    return _origFetch.apply(this, arguments);
  };
})();
</script>
"""


def main() -> int:
    html = SRC.read_text(encoding="utf-8")

    # 1) 在 vue 之前注入 mock
    marker = '<script src="https://cdn.jsdelivr.net/npm/vue@3.4.21'
    if marker not in html:
        print("!! 未找到 vue script 标签，页面结构可能已变化")
        return 1
    html = html.replace(marker, MOCK_SCRIPT + marker, 1)

    # 2) 默认打开设置页
    old = "const tab = ref('dashboard');"
    if old not in html:
        print("!! 未找到 tab 初始值")
        return 1
    html = html.replace(old, "const tab = ref('settings');", 1)

    OUT.write_text(html, encoding="utf-8")
    print(f"已生成预览页: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
