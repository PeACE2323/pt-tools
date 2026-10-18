# PTtools

**PT 站点运维工具箱**——单容器、单端口，把三件日常运维工作合到一起：

| 模块 | 解决什么问题 |
|---|---|
| **CD2 守卫** | CloudDrive2 上传任务秒传判定 / 超时拦截 / 备份重扫重试 |
| **115 监控** | 115 云下载任务超时自动清理 |
| **PT 流量** | qBittorrent / Transmission 上传流量监控与自动限速 |

> 三块能力全部由 **Python 单进程**实现，无子进程、无 Go 编译、无反向代理。
> PT 流量部分移植自 [bt-traffic-guard](https://github.com/PeACE2323/bt-traffic-guard)，
> 协议行为与原 Go 实现保持一致。

## 功能矩阵

### CD2 守卫 + 115 监控

| 模块 | 能力 |
|---|---|
| CD2 上传监控 | 高频轮询 `GetUploadFileList(getAll=true)`，维护 `first_seen_at` |
| **作用域过滤** | **只有命中规则的任务才记录**；如只监控含 `PT` 的源路径，其余任务完全不入库 |
| 规则引擎 | 源路径关键字（支持正则）/ 超时阈值 / 重试次数与间隔，多规则优先级 CRUD |
| 秒传判定 | **实际传输字节数 < 文件大小 1%** 即判为秒传（而非看耗时），写入 `instant_uploads` |
| 不能秒传 | 超时 → `CancelUploadFiles` 取消该文件的上传 → 写入重试队列 |
| **备份重试闭环** | 到点 → `BackupRestartWalkingThrough` 让备份重新扫描，缺失文件自动重传 |
| **Webhook 源路径补全** | 接入 CD2 `file_system_watcher`，用真实 `source_file` 补全 gRPC 缺失的源路径 |
| 115 云下载 | 定期拉取任务列表，超过阈值自动删除离线任务（不删源文件）并写日志 |
| WebUI | 仪表盘 / 规则 / 任务 / 115 云下载 / 事件日志 / PT 策略 / 系统设置 |

### PT 流量监控

| 能力 | 说明 |
|---|---|
| 多客户端 | 同时监控任意数量的 qBittorrent / Transmission 实例 |
| 自定义周期 | 自然日 / 周 / 月 / 小时 / 滚动窗口 |
| 自动限速与恢复 | 达阈值即限速，新周期自动恢复；退出容器时也会恢复，绝不把下载器留在限速状态 |
| 防抖动 | 冷却期，避免在阈值附近反复触发 |
| 通知 | ntfy、通用 Webhook、Discord、Slack |
| 深色 / 浅色主题 | 一键切换 |

> **本系统的上传来源是 CD2 备份任务**。因此重试策略只有一种：取消该文件的
> 上传任务 → 等待间隔 → 触发备份重新扫描。暂停/恢复不会改变能否秒传，
> 所以不提供该分支。


## 目录结构

```text
pt-tools/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI 入口 + 静态挂载 + 生命周期钩子
│   ├── config.py                # 路径/环境变量常量（DATA_DIR、DB 路径等）
│   ├── models.py                # SQLAlchemy 模型 + 建表 + 默认配置播种
│   ├── schemas.py               # Pydantic 请求/响应模型
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py              # 依赖注入（DB 会话、服务单例）
│   │   ├── dashboard.py         # 仪表盘聚合数据
│   │   ├── rules.py             # 规则 CRUD / 启停
│   │   ├── tasks.py             # 实时任务 / 秒传记录 / 重试队列操作
│   │   ├── p115.py              # 云下载列表 / 配置 / 取消日志
│   │   ├── pt.py                # PT 流量接口（/api/pt/*）
│   │   ├── settings.py          # 系统设置 / 连接测试 / Token 获取
│   │   └── webhook.py           # CD2 webhook 接收端点
│   ├── services/
│   │   ├── __init__.py
│   │   ├── rules.py             # 规则匹配纯逻辑（无第三方依赖，可单测）
│   │   ├── webhook_parser.py    # CD2 webhook 数据解析（零依赖）
│   │   ├── webhook.py           # webhook 落库 + 源路径映射
│   │   ├── cd2_client.py        # CD2 gRPC 封装（Bearer / 断线重连 / 任务管理）
│   │   ├── p115_client.py       # 115 封装（异步 / check_response / 单条删除）
│   │   ├── bt_client.py         # BT 下载器协议（qBittorrent / Transmission）
│   │   ├── bt_policy.py         # 限速策略引擎（周期对齐 / 阈值 / 恢复）
│   │   ├── bt_monitor.py        # PT 流量采集编排 + 配置持久化
│   │   └── scheduler.py         # 监控与重试调度引擎（AsyncIOScheduler）
│   └── proto/
│       ├── __init__.py
│       ├── clouddrive.proto     # 内置官方 proto 副本
│       ├── clouddrive_pb2.py        # 构建时生成
│       └── clouddrive_pb2_grpc.py   # 构建时生成
├── static/
│   ├── index.html               # 单页 WebUI（Vue3 + TailwindCSS via CDN）
│   └── vendor/                  # 可选：离线时放置 CDN 兜底资源
├── tools/
│   ├── fetch_proto.py           # 拉取官方 proto + protoc 编译 + 导入修正
│   ├── check_proto.py           # proto 静态自检（无 protobuf 环境可用）
│   ├── check_backend.py         # 后端静态检查（未定义名/死导入/路由一致性）
│   └── check_ui.py              # 前端模板检查（setup 暴露 vs 模板引用）
├── docker/
│   ├── entrypoint.sh            # 数据卷初始化 + 首启编译 proto
│   └── install.sh               # 本地非 Docker 安装
├── docs/
│   ├── schema.sql               # 权威 DDL 参考
│   ├── API_NOTES.md             # 接口事实核查笔记（字段名/陷阱）
│   └── WEBHOOK.md               # CD2 webhook 接入指南
├── data/                        # 挂载卷：guard.db / 115-cookies.txt / logs/
├── tests/
│   ├── __init__.py
│   ├── run_all.py               # 一次跑完全部零依赖测试
│   ├── test_rules.py            # 规则匹配 / 状态机
│   ├── test_webhook.py          # webhook 解析
│   ├── test_scope.py            # 作用域过滤（只监控 PT 场景）
│   ├── test_backup.py           # 备份匹配与重试策略
│   ├── test_cd2_diag.py         # CD2 连接诊断
│   ├── test_timeout_logic.py    # 排队状态不计入超时
│   ├── test_instant_upload.py   # 秒传字节判据
│   ├── test_health.py           # 健康检查与状态维护
│   ├── test_pt_python.py        # PT 流量（客户端/策略/无 Go 残留）
├── requirements.txt
├── requirements-optional.txt
├── Dockerfile                   # 多阶段：Go 编译 + Python 运行时
├── docker-compose.yml
└── README.md
```

## PT 流量监控是怎么实现的

原先是独立的 Go 服务（Traffic Guard），现已**用 Python 重写并并入本进程**：

`	ext
app/services/
├── bt_client.py   下载器协议：qBittorrent / Transmission
├── bt_policy.py   限速策略引擎：周期对齐、阈值判断、限速与恢复
└── bt_monitor.py  采集编排 + 配置持久化（存 SQLite，不再用 YAML）
`

优点：单一技术栈、单进程、无跨进程调用，也不需要 Go 工具链就能构建。

### 采集与判断

`
每 30 秒采集一轮
    ↓
并发拉取各下载器的：上传速度 / 累计上传量 / 种子数 / 连接数 / 当前限速
    ↓
按策略统计「本周期内增量」= 当前累计 - 周期起点累计
    ↓
增量 ≥ 阈值 且 未限速  →  下发限速
    ↓
进入新周期 / 手动解除  →  恢复原始限速
`

**为什么用「累计量差值」而不是「速率积分」**：
速率采样有抖动、丢包，积分误差会累积；客户端自己维护的累计上传字节数
做差最稳。客户端重启会让累计量归零（变小），检测到回退时会重新对齐基线，
不会因此误判。

### 支持的周期

| 对齐方式 | 说明 |
|---|---|
| day | 自然日，每天 00:00 重置 |
| hour | 自然小时 |
| week | 自然周（周一 00:00 起算） |
| month | 自然月 |
| 
olling | 固定长度滚动窗口 |

### 安全设计

**退出时一定解除限速**：容器停止/重启前会主动把所有被限速的下载器恢复原速。
否则用户下次启动时会发现上传莫名很慢，却不知道是上次的限速没解除。

### 配置存放位置

全部在 SQLite 的 pp_settings 表（pt.clients / pt.policies），
随 /app/data 卷持久化。不再有 YAML 文件，界面上可视化编辑。

## 快速开始

### 方式一：直接用预构建镜像（推荐）

镜像由 GitHub Actions 自动构建并发布到 GHCR：

```bash
docker run -d \
  --name pttools \
  --restart unless-stopped \
  -p 8899:8899 \
  -e TZ=Asia/Shanghai \
  -v /path/to/pttools-data:/app/data \
  ghcr.io/peace2323/pt-tools:latest
```

打开 `http://<host>:8899`。

> GHCR 包默认是 private，首次拉取需先登录：
> `echo <你的PAT> | docker login ghcr.io -u PeACE2323 --password-stdin`
> 登录后到 GitHub → Packages → pt-tools → Settings 把可见性改为 Public，
> 之后其他人也能直接拉取。

### 方式二：本地构建

```bash
docker compose up -d --build
```

首次启动后进入「系统设置」填写 CD2 地址与 Token、115 Cookie，点击「连接测试」确认，
再到「CD2 规则」新建一条监控规则即可开始工作。

WebUI 五个页面：

| 页面 | 内容 |
|---|---|
| 仪表盘 | CD2/115 连接状态、今日秒传、当前上传中、等待重试、今日云下载取消、调度任务 |
| CD2 规则 | 规则 CRUD、启停、关键字实时校验与命中预览 |
| 任务监控 | 实时上传列表 / 秒传详情 / 重试队列（支持批量重试、取消、删除） |
| 115 云下载 | 任务列表与驻留时长、超时标红、手动取消、取消日志 |
| 系统设置 | CD2 地址与 Token、账号换 Token、115 Cookie、Webhook、事件日志 |


数据持久化在该目录下的 `./data`，包含：

- `data/guard.db` —— SQLite 数据库
- `data/115-cookies.txt` —— 115 Cookie（权限 600）
- `data/logs/` —— 应用日志

## 关键配置项

在 WebUI「系统设置」中填写，或直接改数据库 `app_settings` 表：

| 键 | 说明 | 默认 |
|---|---|---|
| `cd2.host` | CD2 服务地址 `host:port` | `host.docker.internal:19798` |
| `cd2.token` | CD2 Bearer Token | 空 |
| `cd2.poll_interval` | 上传任务轮询间隔（秒） | 3 |
| `p115.cookie` | 115 Cookie | 空 |
| `p115.poll_interval` | 云下载轮询间隔（秒） | 300 |
| `p115.timeout_threshold` | 云下载最大允许耗时（秒） | 86400 |
| `p115.auto_cancel` | 是否自动取消 | 1 |

> **端口是 19798，不是 19799。**
>
> 这是最容易填错的地方。按 CD2 官方文档：核心服务（含 gRPC）监听 **19798**，
> 与 Web 管理界面同一个端口；**19799 是 HTTPS 管理端口且默认关闭**，
> 不是 gRPC 端口。填 19799 会连接失败。
>
> 该端口需要与你 CD2 容器的 `-p` 映射一致（例如 `-p 19798:19798`）。

## 重试闭环（基于 CD2 备份任务）

本系统的上传由 **CD2 备份任务**产生（源目录 → 云盘目标目录）。因此超时处理是：

```text
上传耗时 > 规则阈值
   ↓
CancelUploadFiles(该文件的 task.key)      ← 只取消这 1 个文件，不动整个备份
   ↓ 等待 retry_interval
BackupGetAll → 找到该文件所属备份
   ↓
BackupRestartWalkingThrough(备份源目录)   ← 让备份重新扫描
   ↓
备份比对目标端 → 缺失文件重新上传 → 再次走秒传判定
```

设计要点：

- **只取消单个任务**，不用 `BackupSetEnabled(false)` 停整个备份，避免波及同一备份下的其他文件；
- **嵌套备份取最长匹配**：`/mnt/PT/movies` 优于 `/mnt/PT`；
- **`/mnt/PT` 不会误匹配 `/mnt/PT2`**（目录前缀已在测试中固化）；
- 找不到所属备份时回退到 `CopyFile`，仍失败则标记 `failed` 并写 `event_logs`。

## 秒传是怎么判定的

**秒传不是「传得快」，而是「根本没传数据」。**

CD2 上传前会先算文件哈希发给 115，若 115 上已存在相同文件
（**任何账号**的网盘里有都算），服务器就不接收数据，直接建一条指向已有文件的记录。

所以本项目的判定依据是**实际传输的字节数**，不是耗时：

```python
transferedBytes / size < 0.01   →  秒传
```

「任务监控 → 秒传详情」有一列 **实际传输**，显示 `0（未传数据）` 即为真秒传，
可以用来验证判定是否正确。

> 早期版本用「耗时 ≤ 阈值」判定，会因排队、算哈希耗时等因素大量误判，
> 导致秒传记录为空、普通上传被误算成秒传。已彻底改为字节判据。

完整原理、常见疑问与排错见 [docs/INSTANT_UPLOAD.md](docs/INSTANT_UPLOAD.md)。

## 作用域过滤（只监控关心的上传）

**只有命中规则的 CD2 上传任务才会被记录和处理**，其余任务完全不进数据库、
不产生任何干预。例如只想监控源路径含 `PT` 的任务，只需建一条规则：

| 字段 | 值 |
|---|---|
| 规则名称 | PT 任务 |
| 源文件夹路径关键字 | `PT` |
| 超时阈值 | 60 秒 |
| 拦截动作 | pause |

于是 `/mnt/local/PT/**` 的上传会被监控，`/mnt/local/Docker/**` 的任务被完全忽略。

> ⚠️ 关键字是**子串**匹配，`PT` 也会命中 `apt`、`PTX` 等路径。
> 需要精确限定目录时，勾选「正则」并填 `/PT/` 或 `^/mnt/local/PT/`。

## CD2 Webhook 接入（推荐）

CD2 的 gRPC 上传任务**不含源路径**，也没有“重新上传本地文件”的 RPC。
接入 `file_system_watcher` webhook 后，两个缺口都能补上：

1. 用真实 `source_file` 参与规则匹配（而不是拿 `destPath` 猜）；
2. `cancel_reupload` 策略可据此真正重新触发上传。

服务端已提供接收端点，在 CD2 配置文件的 `[global_params]` 中把
`base_url` 指向本服务，并启用 `[file_system_watcher]` 即可：

```toml
[global_params]
base_url = "http://192.168.1.10:8899"
enabled = true

[file_system_watcher]
url = "{base_url}/api/webhook/cd2/file_notify?device_name={device_name}&user_name={user_name}"
method = "POST"
enabled = true
# body 使用 CD2 官方模板原样即可，见 docs/WEBHOOK.md
```

完整配置模板、字段语义与安全说明见 [docs/WEBHOOK.md](docs/WEBHOOK.md)。

## 开发

```bash
bash docker/install.sh          # 建 venv、装依赖、编译 proto
. .venv/bin/activate
uvicorn app.main:app --reload --port 8899
```

零依赖单元测试（无需装 sqlalchemy / grpcio / p115client）：

```bash
python tests/run_all.py
```

应用启动冒烟测试（会真实 import 应用并请求关键接口，需要先装依赖）：

```bash
pip install -r requirements.txt
python tools/fetch_proto.py --out app/proto --offline
python tests/test_smoke.py
```

静态检查（同样不依赖第三方库，适合 CI 前置校验）：

```bash
python tools/check_backend.py                    # 未定义名 / 死导入 / 路由一致性
python tools/check_fastapi.py                    # 路由返回注解是否会被当 Pydantic 模型
python tools/check_ui.py static/index.html       # Vue 模板引用完整性
python tools/check_proto.py app/proto/clouddrive.proto
```

单独编译 / 校验 proto：

```bash
python tools/fetch_proto.py --out app/proto           # 拉取官方最新版并编译
python tools/fetch_proto.py --out app/proto --offline # 仅用内置副本编译
python tools/check_proto.py app/proto/clouddrive.proto
```

## 排查

```bash
sqlite3 data/guard.db "select * from event_logs order by id desc limit 20;"
sqlite3 data/guard.db "select status,count(*) from non_instant_tasks group by status;"
docker compose logs -f pttools
```
