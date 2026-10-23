"""全局路径与运行时常量。

所有可变状态都落在 DATA_DIR（Docker 挂载卷 /app/data）下：
    DATA_DIR/guard.db           SQLite 数据库
    DATA_DIR/115-cookies.txt    115 Cookie
    DATA_DIR/logs/              应用日志
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # 项目根
APP_DIR = BASE_DIR / "app"
STATIC_DIR = BASE_DIR / "static"
PROTO_DIR = APP_DIR / "proto"

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
LOG_DIR = DATA_DIR / "logs"

DB_PATH = Path(os.environ.get("GUARD_DB_PATH", DATA_DIR / "guard.db")).resolve()
COOKIE_FILE = Path(
    os.environ.get("P115_COOKIE_FILE", DATA_DIR / "115-cookies.txt")
).resolve()

# SQLite 连接串（WAL + 容忍并发读）
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

# ---------------------------------------------------------------------------
# 默认值（首次启动写入 app_settings）
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, str] = {
    # 默认指向宿主机。CD2 常见三种部署方式对应：
    #   * CD2 跑在宿主机 / NAS 套件     -> host.docker.internal:19798
    #   * CD2 是另一个容器（已映射端口）-> 宿主机IP:19798 或 host.docker.internal:19798
    #   * CD2 与本容器在同一 Docker 网络 -> <CD2容器名>:19798
    #
    # **端口必须是 19798**：CD2 的核心服务端口（含 gRPC）就是 19798，
    # 与 Web 管理界面同一个端口。19799 是 HTTPS 端口且默认关闭，
    # 不是 gRPC 端口 —— 详见 docs/API_NOTES.md「端口」一节。
    "cd2.host": os.environ.get("CD2_DEFAULT_HOST", "host.docker.internal:19798"),
    "cd2.token": "",
    "cd2.username": "",
    "cd2.password": "",
    "cd2.poll_interval": "3",
    "p115.cookie": "",
    "p115.poll_interval": "300",
    "p115.timeout_threshold": str(24 * 3600),
    "p115.auto_cancel": "1",
    "p115.task_page_size": "30",
    # CD2 webhook 鉴权令牌；留空表示不校验（内网快速接入）
    "webhook.token": "",
}

# ---------------------------------------------------------------------------
# CD2 上传任务状态枚举（UploadFileInfo.Status）
# ---------------------------------------------------------------------------
CD2_STATUS_NAMES: dict[int, str] = {
    0: "WaitforPreprocessing",
    1: "Preprocessing",
    2: "Cancelled",
    3: "Transfer",
    4: "Pause",
    5: "Finish",
    6: "Skipped",
    7: "Inqueue",
    8: "Ignored",
    9: "Error",
    10: "FatalError",
}

CD2_STATUS_FINISH = 5          # 已完成（含秒传）
CD2_STATUS_CANCELLED = 2
CD2_STATUS_PAUSE = 4
CD2_STATUS_FAILED = {9, 10}    # Error / FatalError

# “真正在传输数据”的状态：只有这些才会被计入超时判定。
#
# 关键区别（踩过的坑）：
#   Preprocessing(1) / Inqueue(7) / WaitforPreprocessing(0)
#   属于「排队 / 预处理」阶段 —— CD2 有并发上限，任务在队列里等几十分钟是
#   **正常现象**，并不代表「不能秒传」。若把它们计超时，会造成：
#     * 大量误拦截
#     * 秒传记录全部为空（因为耗时都被算成了超阈值）
#     * 用户在 CD2 里看到任务还在正常上传，系统却已判超时
CD2_STATUS_TRANSFERRING = {3}  # Transfer

# 排队类状态：需要单独记录其时长，用于「排队超时」而非「上传超时」判定
CD2_STATUS_QUEUED = {0, 1, 7}  # WaitforPreprocessing / Preprocessing / Inqueue

# 仍算“活跃”（未结束）的全部状态
CD2_STATUS_ACTIVE = CD2_STATUS_TRANSFERRING | CD2_STATUS_QUEUED

# ---------------------------------------------------------------------------
# CD2 守卫判定参数（v4）
# ---------------------------------------------------------------------------
#: 连续多少次轮询观察到「有上传速度」即判定为无法秒传。
#:
#: 秒传的本质是服务器已有该文件、不接收数据，因此秒传任务从不产生
#: 上传流量。相反，只要连续两轮（30s × 2 ≈ 60s）都能观测到字节增长，
#: 就说明它确实在传数据 —— 与排队时长、网络快慢都无关。
CD2_CANCEL_AFTER_HITS = 2

# ---------------------------------------------------------------------------
# 业务状态机
# ---------------------------------------------------------------------------
# non_instant_tasks.status
STATUS_WAITING = "waiting"      # 等待重试
STATUS_RETRYING = "retrying"    # 重试中
STATUS_SUCCESS = "success"      # 重试成功
STATUS_FAILED = "failed"        # 最终失败 / 已放弃
STATUS_CANCELLED = "cancelled"  # 用户取消重试

NON_INSTANT_ACTIVE_STATUSES = (STATUS_WAITING, STATUS_RETRYING)

# 规则：拦截动作 / 重试策略
#
# 本项目上传来自 **CD2 备份任务**，因此重试策略统一为：
#   取消该文件的上传任务 → 等待间隔 → BackupRestartWalkingThrough 让备份重扫
# 暂停/恢复（pause_resume）不适用于备份场景：暂停再恢复只是继续同一次上传，
# 不会改变“能否秒传”的结果，因此不再提供。
INTERCEPT_CANCEL = "cancel"
STRATEGY_CANCEL_REUPLOAD = "cancel_reupload"

# 保留这两个常量仅为兼容旧数据中的历史取值（读取时会被归一化为 cancel_reupload）
INTERCEPT_PAUSE = "pause"
STRATEGY_PAUSE_RESUME = "pause_resume"

# 115 云下载归一化状态
P115_ST_WAITING = "waiting"
P115_ST_DOWNLOADING = "downloading"
P115_ST_FINISHED = "finished"
P115_ST_FAILED = "failed"
P115_ST_CANCELLED = "cancelled"
P115_ST_REMOVED = "removed"

# 115 原始 status 中视为“已完成”的集合
#
# **不要用 status 判定完成**：115 的 status 数值语义在社区资料里
# 说法互相矛盾（有说 2=完成，也有说 2=下载中），实测发现把 status
# 当作完成依据会导致「正在下载的任务被显示成已完成」。
# 这里保留常量仅供排查参考，归一化逻辑以 percentDone 为准。
P115_FINISHED_STATUS: set[int] = set()
# 115 原始 status 中视为“任务已消失/已删除”的集合
P115_REMOVED_STATUS = {-1, 1}

# p115client 云下载列表 stat 参数：0=全部 9=失败 11=完成 12=进行中
P115_STAT_ALL = 0
P115_STAT_FAILED = 9
P115_STAT_FINISHED = 11
P115_STAT_RUNNING = 12

# 115 原始 status 中视为“失败”的集合（stat=9 对应“已失败”）
P115_FAILED_STATUS = {9}

# 115 云下载列表接口基地址（clouddownload.115.com 为主，lixian.115.com 为历史别名）
P115_CLOUDDOWNLOAD_HOST = "https://clouddownload.115.com"
P115_LIXIAN_HOST = "https://lixian.115.com"

# ---------------------------------------------------------------------------
# 调度与容错参数
# ---------------------------------------------------------------------------
# CD2 gRPC 单次调用超时（秒）
CD2_RPC_TIMEOUT = 10.0
# 断开后重连的基础退避（秒）与上限
CD2_RECONNECT_BASE = 2.0
CD2_RECONNECT_MAX = 60.0
# 任务在 CD2 列表中连续消失多少秒后，才认定“已从队列移除”
#
# 分两档：
#   TASK_GONE_FAST_SEC  —— 消失前几乎没传数据（秒传 / 刚结束），快速收敛
#   TASK_GONE_GRACE_SEC —— 消失前仍在传输且有数据量，多等一会儿防抖动
#
# 之前统一用 120 秒，导致 CD2 已完成、面板还显示「上传中」，
# 然后一批任务同时消失，观感很差。
TASK_GONE_FAST_SEC = 12.0
TASK_GONE_GRACE_SEC = 40.0
# 单轮轮询最多处理多少条超时任务（防止积压时一次性打爆 CD2）
MAX_INTERCEPT_PER_ROUND = 20
# 单轮最多触发多少次重试
MAX_RETRY_PER_ROUND = 20
# 秒传记录/事件日志保留条数上限（防止数据库无限膨胀）
MAX_EVENT_LOGS = 5000

LOG_RETENTION_DAYS = 30
