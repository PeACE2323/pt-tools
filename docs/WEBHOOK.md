# CloudDrive2 Webhook 接入说明

> **TL;DR（三分钟搞定）**
>
> 1. 本项目 WebUI → 系统设置 → CD2 Webhook，复制页面上的**完整配置模板**
> 2. 打开 CD2 网页 → 设置 → **Webhook** 页面，粘贴并勾选启用
>    （或编辑 CD2 的 `config.toml` 粘贴同样的内容后重启 CD2）
> 3. 在 CD2 监控的源目录里新建一个文件，等几秒，
>    点 WebUI 上的「查看已收到的源路径记录」—— 出现该文件名即成功
>
> **不配置也能用**，只是源路径匹配会退化为用目标路径推断。

## 为什么需要 webhook

CD2 的 gRPC API 存在一个客观限制：`UploadFileInfo` 只有 `key` 和 `destPath`，
**没有源文件路径**，也**没有"重新上传某个本地文件"的 RPC**。

这导致两个功能缺口：

1. 规则匹配只能拿 `destPath` 去匹配"源文件夹路径关键字"；
2. `cancel_reupload` 策略取消任务后无法重新触发上传。

而 CD2 的 **file_system_watcher webhook** 能在文件变化的第一时间把
`source_file` 推送给我们，正好补上这两个缺口。

---

## 零、最省事的配置方法

在 WebUI 的「系统设置 → CD2 Webhook」区块，直接点「复制」拿到完整模板，
粘贴到下面任一处即可：

| 方式 | 操作位置 | 说明 |
|---|---|---|
| **A（推荐）** | CD2 网页 → 设置 → Webhook | 图形界面，改完立即生效 |
| **B** | CD2 的 `config.toml`（`/Config` 目录） | 改完需重启 CD2 容器 |
| **C** | gRPC `GetWebhookConfigTemplate` | 编程方式获取模板 |

⚠ **地址要对**：模板里的 `base_url` 是 WebUI 当前访问的地址。
如果 CD2 与本项目不在同一台机器/同一网络，需要换成**CD2 能访问到本项目**的地址
（例如本项目所在机器的局域网 IP）。

---

## 一、数据格式（来自 CD2 官方 webhook 配置模板）

### 1. 全局参数

```toml
[global_params]
base_url = "http://localhost:8080"     # 我们的服务地址
enabled = true
time_format = "epoch"                  # 或 "rfc3339" / "utc:%Y-%m-%d %H:%M:%S"

[global_params.default_headers]
content-type = "application/json"
user-agent = "clouddrive2/{version}"
```

### 2. file_system_watcher（本项目使用）

```toml
[file_system_watcher]
url = "{base_url}/api/webhook/cd2/file_notify?device_name={device_name}&user_name={user_name}"
method = "POST"
enabled = true

body = '''
{
    "device_name": "{device_name}",
    "user_name": "{user_name}",
    "version": "{version}",
    "event_category": "{event_category}",
    "event_name": "{event_name}",
    "event_time": "{event_time}",
    "send_time": "{send_time}",
    "data": [
            {
                "action": "{action}",
                "is_dir": "{is_dir}",
                "source_file": "{source_file}",
                "destination_file": "{destination_file}"
            }
    ]
}
'''
```

字段语义（官方注释）：

| 字段 | 含义 |
|---|---|
| `event_category` | 固定 `"file"` |
| `event_name` | 固定 `"notify"` |
| `action` | `create` / `modify` / `delete` / `rename`；`modify` = 向已有文件写入新内容 |
| `is_dir` | `false`=文件，`true`=目录（**注意模板里是字符串 `"false"`**） |
| `source_file` | 动作的源文件路径 |
| `destination_file` | 仅 `action==rename(move)` 时有效 |

`{event_time}` / `{send_time}` 默认是 **epoch 秒**，受 `time_format` 控制。

### 3. mount_point_watcher（可选，本项目暂不消费）

```toml
[mount_point_watcher]
url = "{base_url}/api/webhook/cd2/mount_notify?device_name={device_name}&user_name={user_name}&type={event_name}"
```

字段：`action`(mount/unmount)、`mount_point`、`status`("true"/"false")、`reason`。

---

## 二、本项目的消费方式

### 1. 源路径绑定（解决缺口 1）

webhook 推送 `create` 事件后会带来 `source_file`。我们以**文件名**为键
记录“源路径 → 目标目录”的映射：

```
source_file = /mnt/local/PT/movies/xxx.mkv
                     ↓ (basename = xxx.mkv)
source_paths 表：xxx.mkv → /mnt/local/PT/movies/xxx.mkv
```

之后 gRPC 轮询看到 `destPath = /115/CloudDrive/PT/movies/xxx.mkv` 时，
即可用 `xxx.mkv` 反查真实源路径，命中 `/PT/` 关键字规则。

### 2. 重新触发上传（解决缺口 2）

`cancel_reupload` 策略在需要重传时，按以下顺序取源路径：

1. `source_paths` 表按文件名反查到的最近一次源路径；
2. 记录里已保存的 `source_path`；
3. 都没有 → 判定失败并写 `event_logs`。

拿到源路径后调用 `CopyFile(theFilePaths=[source], destPath=父目录,
conflictPolicy=Skip)` —— 同云内拷贝会重新走上传管线。

---

## 三、安全

* webhook 端点默认要求 `Authorization` 请求头（与 `global_params.default_headers`
  里配置的值一致），可在系统设置中开关。
* 该端点只写 `source_paths` 映射表，不触发任何破坏性操作，
  即使被伪造也无法造成数据损失。
* 若 CD2 与本服务不在同一网络，建议在 `default_headers` 里配 token。

---

## 四、CD2 侧配置示例（可直接粘贴）

把 `base_url` 换成本服务的实际地址（注意 CD2 在容器内访问宿主机时
需要用宿主 IP 或 `host.docker.internal`）：

```toml
[global_params]
base_url = "http://192.168.1.10:8899"
enabled = true

[global_params.default_headers]
content-type = "application/json"
user-agent = "clouddrive2/{version}"

[file_system_watcher]
url = "{base_url}/api/webhook/cd2/file_notify?device_name={device_name}&user_name={user_name}"
method = "POST"
enabled = true
body = '''
{
    "device_name": "{device_name}",
    "user_name": "{user_name}",
    "version": "{version}",
    "event_category": "{event_category}",
    "event_name": "{event_name}",
    "event_time": "{event_time}",
    "send_time": "{send_time}",
    "data": [
            {
                "action": "{action}",
                "is_dir": "{is_dir}",
                "source_file": "{source_file}",
                "destination_file": "{destination_file}"
            }
    ]
}
'''
```
