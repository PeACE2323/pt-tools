# 接口事实核查笔记

本文件记录开发前对两份官方文档与开源库源码的核查结论。凡标注 **已核实** 的
条目，均直接来自官方 proto 或 p115client 源码；标注 **待运行时确认** 的条目，
代码里做了防御性兼容。

---

## 一、CloudDrive2 gRPC

来源：`https://www.clouddrive2.com/api/clouddrive.proto`（官方），
gRPC API 开发者指南 `https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html`。

### 1. 包名与服务名 **已核实**

```protobuf
package clouddrive;
service CloudDriveFileSrv { ... }
```

即完整服务名 `clouddrive.CloudDriveFileSrv`。注意**不是**
`CloudDriveSrv.Protos`（那是 `option csharp_namespace` 的取值，与 gRPC 服务名无关）。

### 2. 认证 **已核实**

除公开方法外，需在 gRPC metadata 携带：

```python
metadata = [("authorization", f"Bearer {token}")]
```

公开（免认证）方法：`GetSystemInfo` `GetToken` `Login`
`LoginWithThirdPartyAccount` `Register` `SendResetAccountEmail`
`ResetAccount` `GetApiTokenInfo` `LoginWith2FA`。

`GetToken` 请求/响应：

```protobuf
message GetTokenRequest {
  string userName = 1;
  string password = 2;
  optional string totpCode = 3;
}
message JWTToken {
  bool success = 1;
  string errorMessage = 2;
  string token = 3;
  google.protobuf.Timestamp expiration = 4;
}
```

### 3. 上传任务（Transfer Tasks）**已核实**

```protobuf
rpc GetUploadFileCount(Empty) returns (GetUploadFileCountResult);
rpc GetUploadFileList(GetUploadFileListRequest) returns (GetUploadFileListResult);
rpc CancelUploadFiles(MultpleUploadFileKeyRequest) returns (Empty);
rpc PauseUploadFiles(MultpleUploadFileKeyRequest) returns (Empty);
rpc ResumeUploadFiles(MultpleUploadFileKeyRequest) returns (Empty);
rpc CancelAllUploadFiles(Empty) returns (Empty);
rpc PauseAllUploadFiles(Empty) returns (Empty);
rpc ResumeAllUploadFiles(Empty) returns (Empty);

message GetUploadFileListRequest {
  bool getAll = 1;
  uint32 itemsPerPage = 2;
  uint32 pageNumber = 3;
  string filter = 4;
  optional UploadFileInfo.Status statusFilter = 5;
  optional UploadFileInfo.OperatorType operatorTypeFilter = 6;
}

message UploadFileInfo {
  string key = 1;               // 任务的唯一 key，Pause/Cancel/Resume 都用它
  string destPath = 2;
  uint64 size = 3;
  uint64 transferedBytes = 4;   // 注意：官方拼写只有一个 r
  string status = 5;            // 人类可读文本
  string errorMessage = 6;
  OperatorType operatorType = 7;
  Status statusEnum = 8;        // 枚举数值，判状态请用这个
}
```

**关键陷阱（已在代码中处理）**

1. `GetUploadFileList` 必须 `getAll=True`，否则只返回分页默认值。
2. 批量请求消息名官方拼写是 `MultpleUploadFileKeyRequest`（少了一个 `i`），
   不要写 `Multiple...`。
3. 生成代码字段名与 proto 逐字一致，**camelCase 保留**：`destPath`、
   `transferedBytes`、`statusEnum`、`operatorType`；不是 snake_case。
4. `statusEnum` 枚举：
   `0 WaitforPreprocessing / 1 Preprocessing / 2 Cancelled / 3 Transfer /
    4 Pause / 5 Finish / 6 Skipped / 7 Inqueue / 8 Ignored / 9 Error / 10 FatalError`
   判定“秒传成功”用 `5 Finish`；`2 Cancelled`、`9/10` 属于我们认为的失败态。
5. `CancelAllUploadFiles` 是异步生效，取消后计数不会立刻归零。
6. 未找到 `PauseUploadFiles` 任务恢复所需的“重新触发上传”专用 RPC
   （官方 proto 中该位置只有注释掉的 `RapidUploadFile*` 说明），因此
   `cancel_reupload` 策略需借助其它已知可创建上传任务的入口（拷贝任务等），
   实现上做了可插拔处理，并在无法重触发时降级为标记最终失败。

   **→ 已由「备份任务」解决，见下方第 6 节。**

### 6. 重新触发上传的正确做法：备份重新扫描 **已核实**

本项目的上传来源是 **CD2 备份任务**（源目录 → 云盘目标目录），
备份过程中 CD2 会为每个文件产生上传传输任务。

因此“取消上传后重新上传”不需要手工构造上传任务，只要让备份重新扫描源目录：

```protobuf
rpc BackupGetAll(google.protobuf.Empty) returns (BackupList);
rpc BackupGetStatus(StringValue) returns (BackupStatus);
rpc BackupRestartWalkingThrough(StringValue) returns (google.protobuf.Empty);
```

用法：

1. `BackupGetAll()` 拿到所有备份，取 `sourcePath` 与已启用的 `destinations`；
2. 用文件源路径匹配所属备份（**取最长匹配**，子备份优先于父备份）；
3. `BackupRestartWalkingThrough(备份源目录)` 触发重扫 —— 备份会重新比对
   目标端，缺失的文件就会重新上传。

`BackupStatus.Status` 枚举：`Idle=0 / WalkingThrough=1 / Error=2 /
Disabled=3 / Scanned=4 / Finished=5 / Waiting=6`。

**注意**：`Backup` 消息的 `FileBackupRule` 有 4 个隐藏开关字段
（proto 编号 100-103）。只设置 `extensions` 而不设 `isEnabled=true`，
规则会静默失效 —— 本项目只读备份配置、不写，因此不受影响，但如果
将来要新增备份需要留意。

### 7. 本项目的策略约束（功能决策）

依据“上传来自备份任务”这一事实，策略做了简化：

- **拦截动作固定为 `cancel`**：取消该文件的单个上传任务，不动整个备份；
- **重试策略固定为 `cancel_reupload`**：到点触发备份重扫；
- **不再提供 `pause_resume`**：暂停再恢复只是继续同一次上传，
  不会改变“能否秒传”的结果，保留它只会增加无效分支。


### 4. 上传任务的“源文件夹路径”问题 **已知限制**

`UploadFileInfo` **只有 `destPath`（目标路径）和 `key`，没有源路径字段**。
因此本系统的“源文件夹路径关键字匹配”按以下顺序取值：

1. 若 `key` 看起来是文件路径（含分隔符），优先用 `key`；
2. 否则用 `destPath`（CD2 的临时上传文件名通常包含源文件信息）；
3. 再否则回退到 `destPath` 的父目录路径。

这点与需求描述的“匹配源文件夹路径”存在语义差距，属于 CD2 API 的客观限制，
不是实现遗漏。

### 5. gRPC 通道 **已核实**

CD2 的 gRPC 端口是**明文 HTTP/2 (h2c)**，需用：

```python
grpc.insecure_channel(target, options=[("grpc.keepalive_time_ms", 30000), ...])
```

### 端口：19798，**不是** 19799 **已核实（曾判断错误）**
来自 CD2 官方帮助文档 `https://www.clouddrive2.com/en/help.html`：

> The core service's management interface runs on **19798** — the same on
> Windows, Linux, macOS, Android and inside Docker.
> There is an HTTPS port too, **19799**. It is off by default — turn it on
> and configure a certificate under Settings.

即：

| 端口 | 用途 | 状态 |
|---|---|---|
| **19798** | 核心服务：Web 管理界面 **+ gRPC**（同一个端口） | 默认开启 |
| 19799 | HTTPS 管理端口 | **默认关闭**，需手动启用 |

官方文档里的 nginx 反代示例也印证了这点：

```nginx
proxy_pass http://127.0.0.1:19798;   # your CD2 HTTP port
```

**本项目曾错误地把 19799 当成 gRPC 端口**，导致用户填 19799 连不上、
填 19798 反而成功。已更正所有默认值与文档。

错误信息要用 `e.details()` 取业务细节。

---

## 二、115 网盘 / p115client

来源：p115client 源码 `client.py`、`tool/clouddownload.py`、`const.py`，
文档 `https://p115client.readthedocs.io/en/latest/`。

### 1. 客户端初始化与响应校验 **已核实**

```python
from p115client import P115Client, check_response

client = P115Client(cookies)                 # cookies 可以是字符串或 Path
resp = await client.xxx(payload, async_=True)
check_response(resp)                          # state 为假时抛 P115OSError
```

`check_response` 是**同步返回 dict 或 awaitable 的同名函数**，
两种写法都成立：`check_response(await call())` 或 `await check_response(call())`。
`errno == 99/911/990001` 对应“请重新登录 / 请验证账号” → 应提示 Cookie 失效。

`async_=True` 需要一个异步 HTTP 封装；本项目用 `httpx_request`，
调用时传 `request=httpx_request.request`。

### 2. 云下载任务接口 **已核实（方法名来自源码）**

源码 `tool/clouddownload.py` 中直接使用了：

- `client.clouddownload_task_list(payload, async_=...)`，
  payload：`{"page": <从 0 开始>, "page_size": int, "stat": 0|9|11|12}`
  响应字段：`count`、`page`、`page_count`、`tasks`
- `client.clouddownload_task_restart(info_hash, async_=...)`
- 任务对象字段（源码与前端脚本互证）：`info_hash`、`name`、`add_time`、
  `status`、`percentDone`、`del_path`、`file_id`、`size`、`move`、`err`、
  `available_actions`、`url`

`stat` 取值（源码类型标注 `Literal[0, 9, 11, 12]`，语义由作者注释给出）：
**0=全部，9=已失败，11=已完成，12=进行中**。

端点基地址（`const.py` 已核实）：`https://clouddownload.115.com`
（历史别名 `https://lixian.115.com`）。

### 3. 任务状态判定 **待运行时确认（已做防御性兼容）**

需求文档写的“`status != 2` 表示未完成”与下面两条证据冲突：

- 前端脚本用 `dataEl.status != -1` 且 `percentDone >= 99` 判完成；
- p115client 用 `stat` 参数在服务端过滤状态。

因此实现里**不单独依赖 `status == 2`**，而是组合判定：

```python
finished = (percent_done >= 99.9) or (status in FINISHED_STATUS_SET)
active   = status != 1 and not finished   # 1 为已删除/不存在
```

并在首次拉取时把原始 `status` / `percentDone` 落库，方便根据实测调整集合。

### 4. 删除离线任务（防坑重点）**已核实端点，payload 采用需求指定格式**

需求指定的单任务表单格式：

```python
data = {"hash[0]": info_hash, "flag": 0}   # 逐个删除，不要传数组
```

端点（`ac=task_del`，来自 115 web lixian 前端脚本）：

- `https://clouddownload.115.com/?ac=task_del`
- 兼容旧路径 `https://lixian.115.com/lixian/?ct=lixian&ac=task_del`

实现策略：**优先**调用封装方法（若当前 p115client 版本提供
`clouddownload_task_del`），失败则回退到 `client.request(...)` 直连单条
`hash[0]` 表单；**绝不**一次性提交 `hash[]` 数组，避免静默失败。
删除只移除离线任务，不删除已落盘的源文件。

### 5. Cookie 持久化

`P115Client(Path("/app/data/115-cookies.txt"))` 形式在收到 405 时可自动扫码并
回写文件；本项目由用户在 WebUI 粘贴 Cookie，服务侧同时写入 DB 与
`/app/data/115-cookies.txt`（chmod 600），重启后自动加载。

---

## 三、仍需在真实环境验证的三件事

1. `clouddownload_task_list` 的 `status` 数值语义（0/1/2/3/4 的具体含义）。
2. 当前 p115client 版本是否存在 `clouddownload_task_del`；不存在时的回退路径。
3. `BackupRestartWalkingThrough` 的实际行为：触发后多久开始重扫、
   是否会重复上传已完成文件（我们依赖其“比对目标端”的增量语义）。

前两处代码已写成“先尝试 → 再回退 → 记录到 event_logs”，不会硬失败。
第三点建议首次上线时用一个测试目录验证一次。
