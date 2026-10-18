# Traffic Guard

一个专为 **Unraid / NAS** 设计的**上传流量监控与自动限速**工具，
适用于 qBittorrent / Transmission 等下载器。

它持续采集 **qBittorrent** 和 **Transmission** 的上传速度与累计流量，
当在自定义周期（每天 / 每周 / 每月 / 滚动窗口）内上传总量达到你设定的阈值时，
**自动下发上传限速**；进入新周期后**自动解除**。

> **典型场景（PT / 私有站点）**：站点有每日或每月的上传量考核，
> 或者你的宽带/机场有流量上限。
> 希望「放开跑、跑够了自动踩刹车」，而不是整天盯着后台手动调速。
>
> 也适用于任何「按流量计费」或「需要限速保平安」的家庭网络环境。

---

## 功能特性

| 功能 | 说明 |
|---|---|
| 多客户端 | 同时监控任意数量的 qBittorrent / Transmission 实例 |
| qBittorrent API Key | 支持 5.2.0+ 的 `Bearer` API Key 认证，也兼容旧版用户名密码，自动选择 |
| 图形化配置 | 所有配置都能在网页上完成，无需预设环境变量或手写 YAML |
| 自定义周期 | 自然日 / 自然周 / 自然月 / 自然小时 / 滚动窗口 |
| 自动限速与恢复 | 达阈值即限速，新周期自动恢复；可指定恢复后的速度；退出容器时也会恢复，绝不把下载器留在限速状态 |
| 防抖动 | 支持冷却期，避免在阈值附近反复触发/解除 |
| 热重载 | 改配置即时生效，**不会**丢失当前周期进度与限速状态 |
| Web 面板 | 实时曲线、客户端明细、策略进度、事件日志、图形化配置 |
| 深色 / 浅色主题 | 一键切换，会记住选择；未选择时跟随系统偏好 |
| 通知 | 支持 ntfy、通用 Webhook、Discord、Slack |
| 预编译二进制 | Releases 提供 amd64 / arm64 / armv7 静态二进制，无需 Go 环境即可打包镜像 |
| 自动构建（规划中） | GitHub Actions 构建双架构镜像到 GHCR，直接 `docker pull`（需先启用，见下文） |
| 轻量 | 单二进制，约 **7.5 MB** 静态编译，无运行时依赖 |

---

## 快速开始（Unraid）

### 方式一：用预编译二进制一键构建镜像（推荐，无需 Go 环境）

仓库的 [Releases](https://github.com/PeACE2323/bt-traffic-guard/releases/latest)
里提供了 **amd64 / arm64 / armv7** 三个架构的静态二进制，配套脚本会自动识别架构、
下载对应文件并打出镜像 —— **不需要安装 Go，也不需要从源码编译**：

```bash
git clone https://github.com/PeACE2323/bt-traffic-guard.git
cd bt-traffic-guard
bash scripts/build-image.sh
```

**NAS 上不方便用 git？** 直接下载 tar 包也一样（脚本不依赖 git）：

```bash
curl -fL -o btg.tar.gz \
  https://github.com/PeACE2323/bt-traffic-guard/archive/refs/heads/main.tar.gz
tar -xzf btg.tar.gz
cd bt-traffic-guard-main
bash scripts/build-image.sh
```

脚本跑完会直接给出 `docker run` 命令。然后打开
`http://<你的Unraid IP>:8080` → **配置** 标签页 →
填写 qBittorrent / Transmission 的地址与凭据 → 保存，立即生效。

> 也可以手动下载二进制直接用（不用 Docker）：
> ```bash
> curl -fL -o bt-traffic-guard \
>   https://github.com/PeACE2323/bt-traffic-guard/releases/latest/download/bt-traffic-guard-linux-amd64
> chmod +x bt-traffic-guard && ./bt-traffic-guard -config ./config.yaml
> ```

### 方式二：手动构建镜像

```bash
git clone https://github.com/PeACE2323/bt-traffic-guard.git
cd bt-traffic-guard
docker build -t bt-traffic-guard:1.0.0 .
```

国内网络构建慢的话：

```bash
docker build --build-arg GOPROXY=https://goproxy.cn,direct -t bt-traffic-guard:1.0.0 .
```

然后启动：

```bash
docker run -d \
  --name bt-traffic-guard \
  --restart unless-stopped \
  -p 8080:8080 \
  -e TZ=Asia/Shanghai \
  -v /mnt/user/appdata/bt-traffic-guard/config:/config \
  -v /mnt/user/appdata/bt-traffic-guard/data:/data \
  bt-traffic-guard:1.0.0
```

**启动后所有配置都在网页上完成，不需要提前设置任何环境变量。**

### 方式三：从 GHCR 拉取（推荐，无需本地构建）

镜像已发布在 GHCR，**支持 linux/amd64 与 linux/arm64 双架构，匿名可拉取**
（不需要 `docker login`）：

```bash
docker run -d \
  --name bt-traffic-guard \
  --restart unless-stopped \
  -p 8080:8080 \
  -e TZ=Asia/Shanghai \
  -v /mnt/user/appdata/bt-traffic-guard/config:/config \
  -v /mnt/user/appdata/bt-traffic-guard/data:/data \
  ghcr.io/peace2323/bt-traffic-guard:latest
```

> **注意镜像地址必须全小写**：GitHub 网页上是 `PeACE2323`，
> 但 GHCR 的镜像路径只接受小写，所以是 `ghcr.io/peace2323/...`。
>
> 如果拉取时报 `denied` / `unauthorized`：到 GitHub 的
> **你的头像 → Packages → bt-traffic-guard → Package settings**
> 把可见性改成 **Public** 即可匿名拉取。

### 方式四：Unraid Docker 模板

1. 把本仓库的 `unraid-template.xml` 上传到 Unraid 的
   `/boot/config/plugins/dockerMan/templates-user/`，并重命名为
   `my-bt-traffic-guard.xml`。
   > 也可以直接在 Unraid 网页 → **Docker** → **Add Container** →
   > 右上角切到 **XML** 视图，把模板内容粘贴进去。

2. 在 **Add Container** 页面的 Template 下拉框中选择 `my-bt-traffic-guard`。

3. 通常只需要填这四项，连接信息全部留空即可：

   | 字段 | 说明 |
   |---|---|
   | **WebUI 端口** | 默认 `8080`，被占用就换一个 |
   | **配置目录** | 默认 `/mnt/user/appdata/bt-traffic-guard/config` |
   | **数据目录** | 默认 `/mnt/user/appdata/bt-traffic-guard/data` |
   | **时区** | 默认 `Asia/Shanghai`。**务必设置正确**，它决定「自然日」从几点开始算 |

4. 点击 **Apply** 启动容器。

5. 浏览器打开 `http://<你的Unraid IP>:8080`，在 **配置** 页填写下载器信息。

> 模板里保留了几个可选的环境变量（如 `BTG_QBITTORRENT_URL`）。
> 它们只在你想「用环境变量强制覆盖配置文件」时才需要填，
> 平时留空即可——留空时以网页/`config.yaml` 里的设置为准。

> ⚠️ **关于地址填写**：如果容器使用 bridge 网络（Unraid 默认），
> qBittorrent 地址**不能填 `127.0.0.1`**，因为那是容器自己的回环地址。
> 请填 Unraid 的局域网 IP，例如 `http://192.168.1.10:8080`。
> 若希望用 `127.0.0.1`，请把容器的网络模式改成 **Host**。

### 方式五：docker-compose

```bash
git clone https://github.com/PeACE2323/bt-traffic-guard.git
cd bt-traffic-guard
docker compose up -d --build
```

`--build` 会从源码构建（不需要 GHCR 上的镜像）。
若已启用 CI，去掉 `--build` 就会直接拉取 GHCR 的预构建镜像。

---

## 配置说明

首次启动会在配置目录自动生成 `config.yaml`。
完整的带注释示例见 [`config.example.yaml`](config.example.yaml)。

编辑方式二选一：

- 直接编辑挂载出来的 `config.yaml`（**保存后 3 秒内自动热重载**）；
- 或者打开 Web 面板 → **配置** 标签页 → 在线编辑 → 点「保存并应用」。

### 单位约定

| 类型 | 单位 | 可用写法 |
|---|---|---|
| 阈值 `threshold_*` | 字节 | `500GiB`、`1.5TB`、`536870912000` |
| 限速 `limit_*` | **字节/秒** | `1048576`（=1 MiB/s）、`1MiB`、`512K`、`0`（不限速） |
| 周期 `window` | 时长 | `30m`、`24h`、`7d`、`30d`、`1w` |

> `GiB` 是 1024 进制，`GB` 是 1000 进制；限速写 `0` 表示完全不限速。

### 最小可用配置

```yaml
clients:
  - name: qbittorrent
    type: qbittorrent
    url: "http://192.168.1.10:8080"
    # 二选一：API Key（5.2.0+）或用户名密码
    api_key: "qbt_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    # username: "admin"
    # password: "adminadmin"

policies:
  - name: daily-500g
    clients: ["*"]
    window: 24h
    align: day            # 自然日，每天 00:00 重置
    threshold_total: 500GiB
    limit_total: 1MiB     # 限速到 1 MiB/s
    resume_on_window_reset: true
    cooldown: 10m         # 触发后至少保持 10 分钟
```

### qBittorrent 认证：API Key 与用户名密码

qBittorrent 从 **v5.2.0** 起引入了 WebUI API Key，本项目两种方式都支持，**填了 API Key 就优先用它**。

| | API Key | 用户名密码 |
|---|---|---|
| 最低版本 | 5.2.0 | 任意 |
| 机制 | `Authorization: Bearer qbt_xxx`，无状态 | `POST /api/v2/auth/login` 换 Cookie |
| 失败处理 | Key 无效直接报错 | 连续失败会**触发 IP 临时封禁** |
| 获取方式 | 设置 → WebUI → API Key → 生成 | 你设置的 WebUI 账号 |

API Key 的格式为 `qbt_` 加 28 位随机字符。生成后填到配置的 `api_key` 字段即可：

```yaml
clients:
  - name: qbittorrent
    type: qbittorrent
    url: "http://192.168.1.10:8080"
    api_key: "qbt_abcdefghijklmnopqrstuvwxyz1234"
```

> - 使用 API Key 时**不会**再调用登录/登出接口（官方明确 API Key 不能用于 `auth` 端点），
>   因此不会因为反复登录而被封 IP。
> - 如果填了 API Key 但 qBittorrent 版本低于 5.2.0，启动时日志会给出明确警告。
> - Key 被轮换后旧 Key 立即失效，此时日志会提示「API Key 无效或已被轮换」，
>   到面板里换一个新的即可。
> - **Transmission 不支持 API Key**，只能用用户名密码（RPC 基本认证）。

### 周期对齐方式 `align`

| 取值 | 含义 |
|---|---|
| `day` | 自然日，每天 00:00 归零（**最常用**，契合「每日上传量考核」） |
| `week` | 自然周，每周一 00:00 归零 |
| `month` | 自然月，每月 1 日 00:00 归零 |
| `hour` | 自然小时 |
| `rolling` | 滚动窗口，从容器启动起算，每 `window` 时长归零一次 |

### 阈值逻辑

`threshold_total` 是本周期允许的上传总量，达到即触发限速。

限速持续到 **进入新周期** 且 **冷却期已过**，然后自动解除限速。

### 解除限速后回到什么速度

由 `resume_limit_total` 控制：

| 取值 | 行为 |
|---|---|
| **留空**（默认） | 恢复到**触发前的原始限速**，通常是完全不限速 |
| `0` | 显式表示不限速 |
| 正值，如 `5MiB` | 恢复到该速度（5 MiB/s） |

**为什么需要这个**：周期结束后如果直接全速放开，很可能几小时就又冲满额度，
于是在「限速 → 满额 → 再限速」之间循环。设一个中间速度更平滑：

```yaml
window: 24h
align: day
threshold_total: 500GiB
limit_total: 1MiB          # 超额后压到 1 MiB/s
resume_limit_total: 20MiB  # 新周期先回到 20 MiB/s，而不是直接放开
cooldown: 10m
```

> `limit_*` 是**触发时**下发的速度；`resume_limit_*` 是**解除时**下发的速度。
> 两者独立，互不影响。

---

## 通知配置

### ntfy（推荐，手机推送最简单）

1. 手机安装 ntfy App，订阅一个 topic（比如 `my-bt-guard`）。
2. 配置：

```yaml
notify:
  enabled: true
  notify_on: ["speedup", "resume", "error"]
  ntfy:
    enabled: true
    url: "https://ntfy.sh"      # 自建填 http://192.168.1.10:8081
    topic: "my-bt-guard"
    priority: "high"
```

### 通用 Webhook / Discord / Slack

```yaml
notify:
  enabled: true
  webhook:
    enabled: true
    url: "https://discord.com/api/webhooks/xxx/yyy"
    format: discord             # generic / discord / slack
```

推送的 JSON 字段（`format: generic`）：
`event`、`title`、`policy`、`hostname`、`time`、`message`、`clients`、`window_bytes`、`threshold`、`old_limit`、`new_limit`。

---

## Web 面板

访问 `http://<NAS IP>:8080`：

- **总览** — 实时上传速度曲线、周期累计进度、限速状态、客户端明细表
- **策略** — 每条策略的周期进度、阈值、当前累计量，可手动「解除限速」或「测试限速」
- **事件日志** — 每次触发/解除/失败都有时间戳与详情
- **配置** — 分四个子页：

| 子页 | 能做什么 |
|---|---|
| **下载器** | 图形化增删改 qBittorrent / Transmission：名称、类型、地址、API Key 或用户名密码、超时、TLS 跳过校验。可实时看到每个下载器的连接状态与当前速度 |
| **限速策略** | 图形化增删改策略：周期、对齐方式、阈值、限速值、解除限速后的速度、冷却期、作用对象 |
| **通用与通知** | 端口、采集间隔、日志级别、时区、面板认证、ntfy 与 Webhook 配置 |
| **高级（YAML）** | 直接编辑 `config.yaml` 原文，适合批量修改或使用表单未暴露的选项 |

**图形化表单与 YAML 是同一份配置**，两边随时可以互相切换，改动都会同步。

> **安全说明（请务必读完，涉及两个行为不同的接口）**
>
> | 接口 | 对应界面 | 是否回传明文凭据 |
> |---|---|---|
> | `/api/config/form` | **下载器** / **限速策略** / **通用与通知** | **不回传**。只返回掩码占位与首尾提示串（如 `qbt_abcd…1234`） |
> | `/api/config/raw` | **高级（YAML）** | **会回传**。这是完整配置文件原文，含明文密码与 API Key |
>
> 前者这样做是为了让用户能确认「某项已配置」而不暴露内容：
> 前端把掩码原样回传时服务端沿用已保存的值，
> 只有你明确点击「清除」或填入新值才会改动。
>
> 后者之所以回传明文，是因为该页面的用途就是让用户
> **看到并编辑自己填过的密码** —— 若不回传就无法修改。
> 因此：**如果你打算把面板暴露到局域网之外，必须开启面板认证**，
> 否则任何能访问该端口的人都能通过这个接口读到你的全部凭据。

设置环境变量 `BTG_WEB_PASSWORD` 即可开启 Basic 认证（用户名默认 `admin`，可用 `BTG_WEB_USERNAME` 修改），
也可以在面板的 **配置 → 通用与通知** 里开启。

---

## 环境变量

**所有配置都可以在网页上完成，环境变量完全是可选的。**
它们只在你想用环境变量强制覆盖配置文件时才有用（优先级高于 YAML）。

| 变量 | 说明 |
|---|---|
| `BTG_CONFIG` | 配置文件路径，默认 `/config/config.yaml` |
| `BTG_LISTEN` | Web 监听地址，默认 `:8080` |
| `BTG_DATA_DIR` | 数据目录，默认 `/data` |
| `BTG_LOG_LEVEL` | `debug` / `info` / `warn` / `error` |
| `BTG_POLL_INTERVAL` | 采集间隔，如 `5s` |
| `BTG_TIMEZONE` | 时区，如 `Asia/Shanghai` |
| `BTG_QBITTORRENT_URL` / `_USERNAME` / `_PASSWORD` / `_API_KEY` | qBittorrent 连接信息 |
| `BTG_TRANSMISSION_URL` / `_USERNAME` / `_PASSWORD` | Transmission 连接信息 |
| `BTG_WEB_PASSWORD` / `BTG_WEB_USERNAME` | 面板认证 |
| `TZ` | 容器时区 |

> ⚠️ 环境变量的优先级**高于** `config.yaml`。
> 如果你设置了 `BTG_QBITTORRENT_URL`，那么在网页上修改这个地址**不会生效**——
> 此时需要把环境变量删掉再改。这也是为什么容器默认不设置这些变量。

---

## 镜像构建（GitHub Actions）

> **✅ 已启用并运行中。**
> 镜像地址：`ghcr.io/peace2323/bt-traffic-guard:latest`

仓库内置的 [`.github/workflows/build.yml`](.github/workflows/build.yml) 会：

- **推送到 `main`** → 跑测试 → 构建并推送 `:latest` 与 `:main`
- **打 `v*` 标签**（如 `v1.0.0`）→ 额外推送 `:1.0.0`、`:1.0`、`:1`
- **Pull Request** → 只跑测试与构建验证，不推送
- **手动触发** → 可自定义标签

每次发布都是 **linux/amd64 + linux/arm64** 双架构，覆盖普通 x86 服务器与 ARM 设备（如树莓派、部分群晖）。

**测试不通过就不会构建镜像**，避免推出去一个坏的版本。

发布一个正式版本的流程：

```bash
git tag v1.0.0
git push origin v1.0.0
```

### 备用分发方式：GitHub Releases

由于上述权限限制，本项目同时提供 **Releases 分发预编译二进制** 的方式（已验证可用）：

- `bt-traffic-guard-linux-amd64` — x86_64 NAS / 服务器
- `bt-traffic-guard-linux-arm64` — 树莓派 4/5、ARM 群晖、Apple Silicon
- `bt-traffic-guard-linux-armv7` — 树莓派 3 等 32 位 ARM 设备

配合 [`scripts/build-image.sh`](scripts/build-image.sh) 与
[`Dockerfile.release`](Dockerfile.release)，用户**无需 Go 环境**即可构建镜像。
详细用法见上面的「方式一」。

### 如果 workflow 没有生效 / 推送被拒绝

GitHub 对 `.github/workflows/` 下的文件有**独立的权限门禁**。
用自动化令牌（CI / API / 脚本）推送时会报：

```
refusing to allow a Personal Access Token to create or update workflow
`.github/workflows/build.yml` without `workflow` scope
```

这个限制**同时作用于 REST API 与 git 推送两条路径**，无法绕过，必须补权限。

仓库自带一个脚本，用你自己的 git 凭据把它推上去：

```bash
bash scripts/install-workflow.sh
```

或者手工处理，三选一：

1. **给令牌补权限**：到 [Personal access tokens](https://github.com/settings/personal-access-tokens)
   编辑该令牌，把 **Workflows** 设为 `Read and write`。
2. **改用 SSH 推送**（SSH 不受该限制）：
   ```bash
   git remote set-url origin git@github.com:PeACE2323/bt-traffic-guard.git
   git push origin main
   ```
3. **在网页上创建**：打开
   [新建 workflow 文件](https://github.com/PeACE2323/bt-traffic-guard/new/main/.github/workflows)，
   文件名填 `build.yml`，把仓库里 `.github/workflows/build.yml` 的内容粘贴进去提交。

> 推上去之后，到仓库的 **Actions** 标签页确认 workflow 出现，
> 也可以点 **Run workflow** 手动触发一次。

---

## 从源码构建

需要 Go 1.24+：

```bash
go build -trimpath -ldflags "-s -w" -o bt-traffic-guard ./cmd/bt-traffic-guard
./bt-traffic-guard -config ./config.yaml
```

交叉编译到 Linux（用于 Docker）：

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o bt-traffic-guard ./cmd/bt-traffic-guard
```

跑测试：

```bash
go test ./...
```

---

## 工作原理

```
   ┌──────────────┐   轮询 API     ┌──────────────────┐
   │ qBittorrent  │◀──────────────│                  │
   └──────────────┘               │  BT Traffic      │
   ┌──────────────┐   轮询 API     │  Guard           │
   │ Transmission │◀──────────────│                  │
   └──────────────┘               │  ┌────────────┐  │
          ▲                       │  │ 流量累计器 │  │
          │  下发限速              │  └────────────┘  │
          └───────────────────────│  ┌────────────┐  │
                                  │  │ 策略状态机 │  │
                                  │  └────────────┘  │
                                  └──────────────────┘
                                           │
                                           ▼
                                    Web 面板 / 通知
```

**流量累计**优先使用客户端自报的 `uploadedBytes` 差值（精确，不受采样抖动影响）；
当客户端重启导致该值归零时，自动退化为「速率 × 时间」的梯形积分，避免出现负差值把累计量冲掉。

**状态机**：`normal` —(累计量 ≥ 阈值)→ `limited` —(进入新周期 且 冷却期已过)→ `normal`。

---
## 为什么不做 IPv4 / IPv6 分开统计

**早期版本尝试过，但结论是：做不到准确，于是移除了。**

原因是两个下载器的公开 API 都只提供**合计**上传量：

- **qBittorrent** 只给 `up_info_data`（总累计）。要分族只能靠 peer 明细，
  而该字段在保种空闲时经常为空 —— 此时任何归因都是瞎猜。
- **Transmission** 同样只给合计值，分族只能按「当前 peer 的地址族占比」分摊。
  这是**估算**：IPv6 peer 断开时，IPv4 那份会被重新分摊而**变小**，
  进而被误判为「客户端重启」，数值回升时又把整段差值当成新增流量 ——
  实测每次 peer 波动可灌入数百 GB 虚假流量（出现过 2 TB 级的错误累计）。

与其给出一个**看起来精确、实际可能错一个数量级**的分族数字，
不如只统计总量 —— 对「每日/每月上传量考核」这个核心用途来说，
总量是**完全准确**的。

> 如果你确实需要精确的按协议族统计，只能在网络层做：
> 在 Unraid 上统计容器 veth 流量，或用 `nftables` / `iftop` 按协议族计数。
> 这超出了仅靠下载器 API 能做到的范围。

---

## 常见问题

**Q：容器启动就退出，或日志报「无法写入 /config」/ 权限不足？**
A：这是容器化部署最常见的问题 —— **挂载的宿主目录属主与容器内运行用户不一致**。

镜像里虽然对 `/config` 与 `/data` 做过 `chown`，但你用 `-v` 挂载宿主目录时，
容器内的目录会被宿主目录**覆盖**，那层权限设置就失效了。
镜像默认以 **uid 1000** 运行，若宿主目录属于 root，就会写不进去。

在 NAS 上执行（路径换成你自己的）：

```bash
sudo chown -R 1000:1000 /mnt/user/appdata/bt-traffic-guard
```

或者让容器以 root 运行：

```bash
# docker run 方式
docker run ... --user 0:0 ...

# docker compose 方式，在 service 下加一行
user: "0:0"
```

> 程序会在启动时检测配置文件是否可写，并在日志里直接打印出对应的 `chown` 命令，
> 不用自己猜。若配置文件可写但数据目录不可写，只会打一条警告，不影响运行。

**Q：日志报「登录 qBittorrent 失败：IP 已被临时封禁」？**
A：qBittorrent 默认在多次登录失败后封禁来源 IP。
先确认账号密码正确，然后在 qBittorrent 设置里解除封禁，或把
`WebUI → 安全 → 封禁前允许的失败次数` 调大。
> 若你的 qBittorrent 是 5.2.0 及以上，建议改用 **API Key** 认证，
> 它是无状态的，不会因为反复登录触发封禁。

**Q：上传速度一直是 0 / 客户端显示异常？**
A：点客户端那一行的「测试」按钮看具体报错。最常见的原因是
容器用了 bridge 网络却把地址填成了 `127.0.0.1`。
另外确认 qBittorrent 的 WebUI 允许来自局域网的连接（可能需要关闭「仅允许本地主机」）。

**Q：改了 `config.yaml` 但没生效？**
A：程序每 3 秒检查一次文件修改时间，稍等几秒即可；
也可以在面板点「配置」→「重新载入」。
如果配置有语法错误，程序会**保留原配置继续运行**并在日志中报错，不会崩溃。

**Q：为什么限速后过一会儿又自动解除了？**
A：这是设计行为——进入新周期后自动恢复。
如果希望触发后一直保持限速，把 `resume_on_window_reset` 设为 `false`。

**Q：周期还没结束就重置了累计量？**
A：检查 `align` 设置。`day` 是自然日（每天 00:00 重置），
不是「从触发时刻起 24 小时」。想要后者请用 `rolling`。
同时确认容器 `TZ` 时区设置正确。

**Q：能不能只监控不限制？**
A：把 `limit_total` 设为 `0`（不限速），此时策略仍会记录事件与推送通知，
但实际不会改变客户端限速（触发时会给出 1 MiB/s 的保守兜底值，
如需纯监控请把对应策略的 `enabled` 设为 `false` 并参考面板曲线）。

**Q：容器停止后下载器还被限速着？**
A：程序在收到 SIGTERM 时会尝试恢复所有处于限速状态的客户端
（若为 `docker stop`，请留出默认 10 秒的停止超时）。

**Q：`docker pull ghcr.io/peace2323/bt-traffic-guard:latest` 报 denied？**
A：GHCR 的包首次发布后可能仍是私有可见性。到 GitHub 的
**你的头像 → Packages → bt-traffic-guard → Package settings → Change visibility** 改成 Public。
或者先 `docker login ghcr.io -u PeACE2323`（密码用有 `read:packages` 权限的 PAT）。

**Q：日志报「API Key 无效或已被轮换」？**
A：在 qBittorrent 里重新生成一个 Key（设置 → WebUI → API Key），
然后到面板的 **配置 → 下载器** 里把新的 Key 粘贴进去保存。
注意生成新 Key 会立即使旧 Key 失效。

**Q：日志报「客户端 xxx 不支持 API Key 认证，该功能需要 5.2.0 及以上版本」？**
A：你的 qBittorrent 版本低于 5.2.0，没有 API Key 机制。
把 `api_key` 清空，改用用户名密码即可（或者在面板里清除 API Key 并填上账号密码）。

**Q：面板上改了地址，但好像没生效？**
A：检查是否设置了对应的环境变量（如 `BTG_QBITTORRENT_URL`）。
**环境变量优先级高于配置文件**，如果设置了，网页上的修改会被它覆盖。
把该环境变量删掉再改即可。容器默认不设置这些变量，就是为了避免这个坑。

**Q：在面板上能改密码吗？会不会把密码显示出来？**
A：分两种情况：

- **下载器 / 限速策略 / 通用与通知** 这三个子页**不会回显**，
  只返回掩码和首尾提示串（如 `qbt_abcd…1234`）。
  只要不动那个输入框，保存时就会沿用已保存的值；
  想换新值就直接输入，想彻底清除就点旁边的「清除」按钮。
- **高级（YAML）** 子页**会显示明文** —— 因为那一页就是完整的
  配置文件原文，用途是让你能看到并修改自己填过的内容。

所以：**若面板可被外网访问，请务必开启面板认证**，
否则任何人都能通过 YAML 那一页读到你的全部凭据。

---

## 项目结构

```
bt-traffic-guard/
├── cmd/bt-traffic-guard/main.go      # 程序入口、组件装配、热重载
├── internal/
│   ├── config/                       # 配置加载、校验、周期边界计算
│   ├── client/                       # qBittorrent / Transmission 客户端
│   ├── traffic/                      # 流量累计与实时曲线
│   ├── policy/                       # 限速状态机
│   ├── notify/                       # ntfy / Webhook 通知
│   └── web/                          # HTTP 服务、图形化表单与 Web 面板
├── .github/workflows/build.yml       # 自动测试并发布镜像到 GHCR
├── Dockerfile
├── docker-compose.yml
├── unraid-template.xml               # Unraid Docker 模板
├── config.example.yaml               # 带注释的配置示例
└── README.md
```

---

## 许可证

MIT
