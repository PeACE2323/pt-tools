"""CloudDrive2 gRPC 客户端封装。

严格遵循官方 gRPC API 指南与 clouddrive.proto：

* 服务全名 ``clouddrive.CloudDriveFileSrv``
* 除公开方法外，所有调用需携带 metadata ``("authorization", "Bearer <token>")``
* ``GetToken(userName, password)`` 可换取 JWT（免认证）
* 传输任务：``GetUploadFileList`` 必须 ``getAll=True`` 才是全量
* 生成代码字段名为 camelCase：``destPath`` / ``transferedBytes`` / ``statusEnum``

本模块的设计要点：

1. **同步 gRPC 跑在线程池**：grpcio 的 stub 是同步阻塞的，直接放进 asyncio
   事件循环会卡死调度器。所有调用统一经 ``asyncio.to_thread`` 投递。
2. **断线自动重连**：channel 失效时按指数退避重建，调用失败自动重试一次。
3. **单一 channel 复用**：每个目标地址只维持一个 channel。
4. **proto 延迟编译**：若 ``clouddrive_pb2*`` 尚未生成，给出明确的运维提示，
   而不是抛 ImportError 堆栈。
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import (
    CD2_RECONNECT_BASE,
    CD2_RECONNECT_MAX,
    CD2_RPC_TIMEOUT,
    CD2_STATUS_NAMES,
)
from ..utils import get_logger, log_event, utcnow_ts

logger = get_logger("guard.cd2")

# BackupStatus.Status 枚举名（与 proto 保持一致）
BACKUP_STATUS_NAMES: dict[int, str] = {
    0: "Idle",
    1: "WalkingThrough",
    2: "Error",
    3: "Disabled",
    4: "Scanned",
    5: "Finished",
    6: "Waiting",
}


# ===========================================================================
# 异常
# ===========================================================================
class CD2Error(Exception):
    """CD2 调用相关异常基类。"""


class CD2NotConfigured(CD2Error):
    """未配置地址或 Token。"""


class CD2Unavailable(CD2Error):
    """连接不上 CD2（网络/端口/服务未启动）。"""


class CD2AuthError(CD2Error):
    """Token 无效或过期（UNAUTHENTICATED / PERMISSION_DENIED）。"""


class CD2ProtoMissing(CD2Error):
    """proto 生成代码缺失。"""


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass(slots=True)
class UploadTask:
    """CD2 上传任务的规范化视图。

    对应 proto 的 ``UploadFileInfo``；源路径不在 proto 中，这里按
    key → destPath → destPath 父目录 的优先级推导，见 ``source_path``。
    """

    key: str
    dest_path: str
    size: int
    transferred_bytes: int
    status_text: str
    status_enum: int
    operator_type: int
    error_message: str
    # 由 CD2 file_system_watcher webhook 补全的真实源路径（无则不覆盖推导值）
    source_override: str = ""

    @property
    def status_name(self) -> str:
        return CD2_STATUS_NAMES.get(self.status_enum, f"Unknown({self.status_enum})")

    @property
    def file_name(self) -> str:
        """从目标路径取文件名（webhook 映射也按此名对齐）。"""
        if not self.dest_path:
            return self.key
        name = self.dest_path.rstrip("/").rsplit("/", 1)[-1]
        return name or self.key

    def set_source_override(self, path: str) -> None:
        """写入 webhook 提供的真实源路径。"""
        if path:
            self.source_override = path

    @property
    def source_path(self) -> str:
        """还原“源路径”。

        CD2 的 ``UploadFileInfo`` 不含源路径字段（API 客观限制），所以按以下
        优先级推导：

        1. webhook 补全的真实源路径（最可靠）；
        2. 若 ``key`` 形如路径（含 ``/`` 或 ``\\``），用 ``key``；
        3. 否则退回 ``dest_path``。
        """
        if self.source_override:
            return self.source_override
        if self.key and ("/" in self.key or "\\" in self.key):
            return self.key
        if self.dest_path:
            return self.dest_path
        return self.key

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(100.0, self.transferred_bytes / self.size * 100.0)

    # ------------------------------------------------------------------
    # 秒传判定
    # ------------------------------------------------------------------
    # 原理（CD2 官方文档 + 通用秒传机制）：
    #   上传前客户端先算出文件的哈希值发给服务器；服务器发现该文件
    #   已存在（任何用户的网盘里），就**不再接收文件数据**，
    #   只建一条指向已有数据的记录。因此真正的秒传：
    #       * 客户端实际传输的字节数 ≈ 0
    #       * 从开始到完成只花掉「算哈希 + 预处理」的时间
    #
    # 所以判断秒传最可靠的依据不是「耗时是否小于阈值」，
    # 而是 **是否真的传了数据**。耗时只作为辅助（防止把 0 字节的空文件误判）。

    @property
    def transferred_ratio(self) -> float:
        """已传输字节占文件大小的比例（0~1）。"""
        if self.size <= 0:
            return 0.0
        return min(1.0, self.transferred_bytes / self.size)

    @property
    def is_instant_by_bytes(self) -> bool:
        """按「是否真的传了数据」判定秒传。

        秒传的任务在完成时 ``transferedBytes`` 会远小于 ``size``
        （通常为 0）。这里用 1% 作为容差，兼容 CD2 在预处理阶段
        可能统计到的少量内部读取。
        """
        if self.size <= 0:
            # 没有大小信息时无法用字节判定，交由调用方用耗时兜底
            return True
        return self.transferred_ratio < 0.01

    @property
    def is_real_upload_by_bytes(self) -> bool:
        """明确发生过实际数据传输（即非秒传）。"""
        return not self.is_instant_by_bytes


@dataclass(slots=True)
class CD2Status:
    """连接/认证状态快照，供仪表盘展示。"""

    configured: bool = False
    connected: bool = False
    authenticated: bool = False
    host: str = ""
    user_name: str = ""
    system_ready: bool = False
    product_version: str = ""
    upload_count: int = 0
    message: str = ""
    last_error: str = ""
    checked_at: float = field(default_factory=utcnow_ts)


# ===========================================================================
# proto 延迟加载
# ===========================================================================
class _ProtoHolder:
    """延迟导入生成的 pb2 / pb2_grpc 模块。"""

    _pb2: Any = None
    _pb2_grpc: Any = None
    _error: str | None = None

    @classmethod
    def load(cls) -> tuple[Any, Any]:
        if cls._pb2 is not None and cls._pb2_grpc is not None:
            return cls._pb2, cls._pb2_grpc
        if cls._error is not None:
            raise CD2ProtoMissing(cls._error)
        try:
            cls._pb2 = importlib.import_module(
                "app.proto.clouddrive_pb2"
            )
            cls._pb2_grpc = importlib.import_module(
                "app.proto.clouddrive_pb2_grpc"
            )
        except Exception as exc:  # noqa: BLE001
            cls._error = (
                "clouddrive_pb2 / clouddrive_pb2_grpc 未生成，请先执行："
                "python tools/fetch_proto.py --out app/proto "
                f"（原始错误: {exc.__class__.__name__}: {exc}）"
            )
            raise CD2ProtoMissing(cls._error) from exc
        return cls._pb2, cls._pb2_grpc


# ===========================================================================
# gRPC 错误归类
# ===========================================================================
_AUTH_CODES = {16}  # grpc.StatusCode.UNAUTHENTICATED
_PERM_CODES = {7}   # grpc.StatusCode.PERMISSION_DENIED
_UNAVAILABLE_CODES = {14}  # grpc.StatusCode.UNAVAILABLE


def _grpc_code_of(exc: BaseException) -> int | None:
    """尽力取出 status code 的数值，避免硬依赖 grpc 类型判等。"""
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            status = code()
            value = getattr(status, "value", None)
            if isinstance(value, tuple) and value:
                return int(value[0])
        except Exception:  # noqa: BLE001
            return None
    return None


def _grpc_details(exc: BaseException) -> str:
    """取业务细节。官方指南建议用 ``e.details()`` 而不是 ``str(e)``。"""
    details = getattr(exc, "details", None)
    if callable(details):
        try:
            text = details()
            if text:
                return str(text)
        except Exception:  # noqa: BLE001
            pass
    return f"{exc.__class__.__name__}: {exc}"


# ===========================================================================
# 客户端
# ===========================================================================
class CD2Client:
    """CloudDrive2 gRPC 异步封装。

    用法::

        client = CD2Client()
        await client.configure("clouddrive2:19798", token="xxx")
        await client.connect()
        tasks = await client.list_upload_tasks()

        # 超时拦截：取消该文件的上传任务
        await client.cancel_uploads([t.key for t in timed_out])

        # 到点重试：让备份重新扫描源目录
        await client.restart_backup_walkthrough("/mnt/local/PT")
    """

    def __init__(self) -> None:
        self._host: str = ""
        self._token: str = ""
        self._username: str = ""
        self._password: str = ""
        self._channel: Any = None
        self._stub: Any = None
        self._lock = asyncio.Lock()
        self._reconnect_attempts = 0
        self._status = CD2Status()

    # ---------------------------------------------------------------- 配置
    async def configure(
        self,
        host: str,
        token: str = "",
        username: str = "",
        password: str = "",
    ) -> None:
        """更新连接参数。地址或凭据变化时重建 channel。"""
        if self._host != host or self._token != token:
            await self.close()
        self._host = (host or "").strip()
        self._token = (token or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""
        self._status.host = self._host

    @property
    def host(self) -> str:
        return self._host

    @property
    def status(self) -> CD2Status:
        return self._status

    # ---------------------------------------------------------------- 通道
    @staticmethod
    def _normalize_target(host: str) -> str:
        """把用户输入的 host 规整成 grpc target。

        接受 ``host:port``、``http://host:port``、``https://host:port``，
        默认补齐 19798 端口（CD2 核心服务端口，gRPC 与此同端口）。
        """
        target = (host or "").strip()
        if not target:
            raise CD2NotConfigured("CD2 地址为空")
        for prefix in ("http://", "https://", "grpc://", "grpcs://"):
            if target.startswith(prefix):
                target = target[len(prefix):]
        target = target.rstrip("/")
        # IPv6 字面量 [::1] 已带方括号
        if target.startswith("["):
            if "]:" not in target and not target.endswith("]"):
                raise CD2NotConfigured(f"无法解析 CD2 地址: {host}")
            if target.endswith("]"):
                target = f"{target}:19798"
            return target
        if ":" not in target:
            target = f"{target}:19798"
        return target

    async def _build_channel(self) -> None:
        if not self._host:
            raise CD2NotConfigured("尚未配置 CD2 地址")

        pb2, pb2_grpc = _ProtoHolder.load()
        del pb2  # 仅用于触发加载与校验

        try:
            import grpc  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise CD2Unavailable("未安装 grpcio") from exc

        target = self._normalize_target(self._host)

        # CD2 的 gRPC 是明文 h2c，必须 insecure_channel
        options = [
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
        ]
        self._channel = grpc.insecure_channel(target, options=options)
        self._stub = pb2_grpc.CloudDriveFileSrvStub(self._channel)
        logger.info("CD2 channel 已创建: %s", target)

    async def connect(self, force: bool = False) -> None:
        """确保 channel 可用（惰性创建 + 断线重建）。"""
        async with self._lock:
            if self._stub is not None and not force:
                return
            if force and self._channel is not None:
                await self.close()
            await self._build_channel()
            self._reconnect_attempts = 0

    async def close(self) -> None:
        """关闭 channel。"""
        channel = self._channel
        self._channel = None
        self._stub = None
        if channel is not None:
            try:
                await asyncio.to_thread(channel.close)
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 CD2 channel 异常（忽略）: %s", exc)

    def _metadata(self) -> list[tuple[str, str]]:
        """构造 gRPC metadata；无 token 时返回空（仅供公开方法使用）。"""
        if not self._token:
            return []
        return [("authorization", f"Bearer {self._token}")]

    # ------------------------------------------------------------ 调用封装
    async def _call(
        self,
        method_name: str,
        request: Any,
        *,
        require_auth: bool = True,
        timeout: float = CD2_RPC_TIMEOUT,
        retry_on_reconnect: bool = True,
    ) -> Any:
        """统一调用入口：鉴权校验 + 线程池执行 + 失败重连重试。"""
        if require_auth and not self._token:
            raise CD2AuthError("尚未配置 CD2 API Token")

        await self.connect()
        stub = self._stub
        if stub is None:  # pragma: no cover
            raise CD2Unavailable("CD2 stub 未初始化")

        method = getattr(stub, method_name, None)
        if method is None:
            raise CD2Error(f"CD2 服务不提供方法: {method_name}")

        metadata = self._metadata()

        def _invoke() -> Any:
            return method(request, metadata=metadata, timeout=timeout)

        try:
            return await asyncio.to_thread(_invoke)
        except BaseException as exc:  # noqa: BLE001
            code = _grpc_code_of(exc)
            details = _grpc_details(exc)

            if code in _AUTH_CODES or code in _PERM_CODES:
                self._status.authenticated = False
                self._status.last_error = details
                raise CD2AuthError(f"CD2 鉴权失败: {details}") from exc

            # 主机名解析失败属于配置错误，重试无意义，直接给出可操作建议
            if self._is_name_resolution_error(details):
                message = self.diagnose_connection_error(self._host, details)
                self._status.connected = False
                self._status.last_error = message
                raise CD2Unavailable(message) from exc

            if code in _UNAVAILABLE_CODES and retry_on_reconnect:
                self._reconnect_attempts += 1
                delay = min(
                    CD2_RECONNECT_MAX,
                    CD2_RECONNECT_BASE * (2 ** (self._reconnect_attempts - 1)),
                )
                logger.warning(
                    "CD2 不可用(%s)，%.1fs 后重连（第 %d 次）: %s",
                    method_name, delay, self._reconnect_attempts, details,
                )
                await asyncio.sleep(delay)
                try:
                    await self.connect(force=True)
                    return await self._call(
                        method_name,
                        request,
                        require_auth=require_auth,
                        timeout=timeout,
                        retry_on_reconnect=False,
                    )
                except CD2Error:
                    raise
                except BaseException as exc2:  # noqa: BLE001
                    detail2 = _grpc_details(exc2)
                    raise CD2Unavailable(
                        self.diagnose_connection_error(self._host, detail2)
                    ) from exc2

            raise CD2Unavailable(
                self.diagnose_connection_error(self._host, details)
            ) from exc
    @staticmethod
    def diagnose_connection_error(host: str, detail: str) -> str:
        """把底层 gRPC 错误翻译成可操作的建议。

        最常见的三类失败：

        * ``address lookup failed`` / ``Domain name not found``
          —— 主机名解析不了（容器名写错，或不在同一 Docker 网络）
        * ``Connection refused``
          —— 域名能解析但端口没监听（CD2 未映射 gRPC 端口）
        * ``Deadline Exceeded``
          —— 端口通了但不响应（可能不是 gRPC 端口）
        """
        lowered = (detail or "").lower()
        target = host or "(未配置)"

        if any(
            k in lowered
            for k in (
                "lookup failed",
                "domain name not found",
                "no such host",
                "errors resolving",
                "name resolution",
                "unknown host",
            )
        ):
            return (
                f"主机名解析失败：无法解析 {target} 这个地址。\n"
                "常见原因与解决办法：\n"
                "1) 容器名写错，或 CD2 容器与本容器不在同一个 Docker 网络。\n"
                "   若 CD2 是另一个容器：可用宿主机 IP，或把两个容器加入同一网络"
                "（docker network connect <net> <container>）后用 <CD2容器名>:19798。\n"
                "2) 若 CD2 在宿主机/NAS 上：用 host.docker.internal:19798，\n"
                "   并在 docker run 时加 --add-host host.docker.internal:host-gateway。\n"
                "3) 不要填 http:// 前缀，只填 host:port。"
            )

        if "connection refused" in lowered:
            return (
                f"连接被拒绝：{target} 能解析，但该端口没有服务在监听。\n"
                "请确认：\n"
                "1) 端口应为 CD2 核心服务的 **19798**（gRPC 与 Web 同端口）。\n"
                "   19799 是 HTTPS 管理端口且默认关闭，不是 gRPC 端口。\n"
                "2) CD2 容器已映射该端口（docker run 加 -p 19798:19798）。\n"
                "3) CD2 服务确实在运行。"
            )

        if "deadline" in lowered or "timeout" in lowered:
            return (
                f"连接超时：{target} 无响应。\n"
                "请确认端口是 19798（核心服务，gRPC 同端口），且没有被防火墙拦截。"
            )

        return f"无法连接 {target}：{detail}"

    @staticmethod
    def _is_name_resolution_error(detail: str) -> bool:
        """判断是否为 DNS 解析类错误（这类错误重试无意义）。

        覆盖 gRPC 在不同版本/平台下的几种措辞，包括真实的：
            errors resolving clouddrive2:19798:
            [field:hostname lookup error:address lookup failed for
             clouddrive2:19798: Domain name not found]
        """
        lowered = (detail or "").lower()
        return any(
            k in lowered
            for k in (
                "lookup failed",
                "domain name not found",
                "no such host",
                "errors resolving",
                "name resolution",
                "unknown host",
            )
        )

    # ------------------------------------------------------------ 认证
    def mark_status(
        self,
        *,
        connected: bool | None = None,
        authenticated: bool | None = None,
        user_name: str | None = None,
        upload_count: int | None = None,
        message: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """由定时任务更新连接状态快照。

        仪表盘读的是 ``status``，而 ``test_connection()`` 只在用户手动
        点击测试时才会更新它 —— 这导致重启后仪表盘一直显示「失败」，
        直到用户手动点一次。定时轮询成功后也应更新这里。
        """
        if connected is not None:
            self._status.connected = connected
        if authenticated is not None:
            self._status.authenticated = authenticated
        if user_name is not None:
            self._status.user_name = user_name
        if upload_count is not None:
            self._status.upload_count = upload_count
        if message is not None:
            self._status.message = message
        if last_error is not None:
            self._status.last_error = last_error
        self._status.host = self._host
        self._status.checked_at = utcnow_ts()

    async def fetch_token(self, username: str, password: str) -> str:
        """调用 ``GetToken`` 用账号密码换取 JWT（公开方法，无需 Bearer）。"""
        pb2, _ = _ProtoHolder.load()
        await self.connect()

        request = pb2.GetTokenRequest(userName=username, password=password)
        resp = await self._call(
            "GetToken", request, require_auth=False, retry_on_reconnect=False
        )
        success = bool(getattr(resp, "success", False))
        token = getattr(resp, "token", "") or ""
        error = getattr(resp, "errorMessage", "") or ""
        if not success or not token:
            raise CD2AuthError(f"获取 Token 失败: {error or '未知原因'}")

        self._token = token
        self._status.authenticated = True
        logger.info("已通过账号密码获取 CD2 Token")
        return token

    async def get_system_info(self) -> dict[str, Any]:
        """``GetSystemInfo``（公开方法），用于连接测试。"""
        pb2, _ = _ProtoHolder.load()
        from google.protobuf.empty_pb2 import Empty

        del pb2
        resp = await self._call(
            "GetSystemInfo", Empty(), require_auth=False, retry_on_reconnect=False
        )
        return {
            "is_login": bool(getattr(resp, "IsLogin", False)),
            "user_name": getattr(resp, "UserName", "") or "",
            "system_ready": bool(getattr(resp, "SystemReady", False)),
            "system_message": getattr(resp, "SystemMessage", "") or "",
            "has_error": bool(getattr(resp, "hasError", False)),
        }

    async def test_connection(self) -> dict[str, Any]:
        """连接测试：GetSystemInfo →（有 Token 时）上传计数。"""
        result: dict[str, Any] = {
            "ok": False,
            "host": self._host,
            "message": "",
        }
        self._status = CD2Status(host=self._host)
        try:
            info = await self.get_system_info()
            self._status.connected = True
            self._status.user_name = info["user_name"]
            self._status.system_ready = info["system_ready"]
            self._status.message = info["system_message"]

            result.update(
                {
                    "ok": True,
                    "user_name": info["user_name"],
                    "system_ready": info["system_ready"],
                    "system_message": info["system_message"],
                    "is_login": info["is_login"],
                }
            )

            if self._token:
                try:
                    counts = await self.get_all_tasks_count()
                    self._status.authenticated = True
                    self._status.upload_count = counts.get("upload_count", 0)
                    result["authenticated"] = True
                    result["upload_count"] = counts.get("upload_count", 0)
                except CD2AuthError as exc:
                    self._status.authenticated = False
                    self._status.last_error = str(exc)
                    result["authenticated"] = False
                    result["message"] = f"gRPC 可连通，但 Token 无效: {exc}"
            else:
                result["authenticated"] = False
                result["message"] = "gRPC 可连通，但未配置 Token（无法读取任务）"

            self._status.last_error = ""
            return result

        except CD2Error as exc:
            self._status.connected = False
            self._status.last_error = str(exc)
            result["message"] = str(exc)
            result["error_type"] = exc.__class__.__name__
            return result
        finally:
            self._status.checked_at = utcnow_ts()

    # ------------------------------------------------------------ 任务查询
    async def get_all_tasks_count(self) -> dict[str, int]:
        """``GetAllTasksCount``。"""
        from google.protobuf.empty_pb2 import Empty

        resp = await self._call("GetAllTasksCount", Empty())
        return {
            "download_count": int(getattr(resp, "downloadCount", 0)),
            "upload_count": int(getattr(resp, "uploadCount", 0)),
            "copy_task_count": int(getattr(resp, "copyTaskCount", 0)),
        }

    async def list_upload_tasks(self, filter_text: str = "") -> list[UploadTask]:
        """``GetUploadFileList``，**强制 getAll=True** 取全量任务。

        注意：官方 proto 注释指出 ``getAll`` 为真时才返回全部任务；
        同时仍显式给一个足够大的 itemsPerPage 作为兼容兜底。
        """
        pb2, _ = _ProtoHolder.load()

        request = pb2.GetUploadFileListRequest(
            getAll=True,
            itemsPerPage=10000,
            pageNumber=1,
            filter=filter_text or "",
        )
        resp = await self._call("GetUploadFileList", request)

        tasks: list[UploadTask] = []
        for item in getattr(resp, "uploadFiles", []):
            tasks.append(
                UploadTask(
                    key=getattr(item, "key", "") or "",
                    dest_path=getattr(item, "destPath", "") or "",
                    size=int(getattr(item, "size", 0) or 0),
                    transferred_bytes=int(getattr(item, "transferedBytes", 0) or 0),
                    status_text=getattr(item, "status", "") or "",
                    status_enum=int(getattr(item, "statusEnum", 0) or 0),
                    operator_type=int(getattr(item, "operatorType", 0) or 0),
                    error_message=getattr(item, "errorMessage", "") or "",
                )
            )
        logger.debug("CD2 上传任务 %d 条", len(tasks))
        return tasks

    # ------------------------------------------------------------ 任务操作
    async def _batch_keys(self, method_name: str, keys: Iterable[str]) -> int:
        """对一批 key 执行 Pause/Resume/Cancel。返回成功提交的数量。"""
        pb2, _ = _ProtoHolder.load()
        key_list = [k for k in keys if k]
        if not key_list:
            return 0
        request = pb2.MultpleUploadFileKeyRequest(keys=key_list)
        await self._call(method_name, request)
        logger.info("CD2 %s 已提交 %d 个任务", method_name, len(key_list))
        return len(key_list)

    async def pause_uploads(self, keys: Iterable[str]) -> int:
        """``PauseUploadFiles`` —— 暂停上传（可原地恢复）。"""
        return await self._batch_keys("PauseUploadFiles", keys)

    async def resume_uploads(self, keys: Iterable[str]) -> int:
        """``ResumeUploadFiles`` —— 恢复被暂停的上传。"""
        return await self._batch_keys("ResumeUploadFiles", keys)

    async def cancel_uploads(self, keys: Iterable[str]) -> int:
        """``CancelUploadFiles`` —— 取消上传（任务会移出队列）。"""
        return await self._batch_keys("CancelUploadFiles", keys)

    async def cancel_all_uploads(self) -> None:
        from google.protobuf.empty_pb2 import Empty

        await self._call("CancelAllUploadFiles", Empty())

    async def pause_all_uploads(self) -> None:
        from google.protobuf.empty_pb2 import Empty

        await self._call("PauseAllUploadFiles", Empty())

    async def resume_all_uploads(self) -> None:
        from google.protobuf.empty_pb2 import Empty

        await self._call("ResumeAllUploadFiles", Empty())

    # ------------------------------------------------------------ 备份任务
    async def list_backups(self) -> list[dict[str, Any]]:
        """``BackupGetAll`` —— 列出所有备份任务。

        备份任务是本项目上传的来源：备份源目录 → 云盘目标目录时，
        CD2 会为每个文件产生上传传输任务。
        """
        from google.protobuf.empty_pb2 import Empty

        resp = await self._call("BackupGetAll", Empty())
        backups: list[dict[str, Any]] = []
        for item in getattr(resp, "backups", []):
            backups.append(
                {
                    "source_path": getattr(item, "sourcePath", "") or "",
                    "destinations": [
                        {
                            "path": getattr(d, "destinationPath", "") or "",
                            "enabled": bool(getattr(d, "isEnabled", False)),
                        }
                        for d in getattr(item, "destinations", [])
                    ],
                    "enabled": bool(getattr(item, "isEnabled", False)),
                    "fs_watch": bool(getattr(item, "fileSystemWatchEnabled", False)),
                }
            )
        return backups

    async def get_backup_status(self, source_path: str) -> dict[str, Any] | None:
        """``BackupGetStatus`` —— 查询某备份任务的实时状态。"""
        pb2, _ = _ProtoHolder.load()
        try:
            resp = await self._call(
                "BackupGetStatus", pb2.StringValue(value=source_path)
            )
        except CD2Error as exc:
            logger.debug("查询备份状态失败 %s: %s", source_path, exc)
            return None

        status_enum = int(getattr(resp, "status", 0) or 0)
        return {
            "source_path": getattr(resp, "sourcePath", "") or source_path,
            "status": status_enum,
            "status_name": BACKUP_STATUS_NAMES.get(status_enum, f"Unknown({status_enum})"),
            "total_files": int(getattr(resp, "totalFiles", 0) or 0),
            "processed_files": int(getattr(resp, "processedFiles", 0) or 0),
            "failed_files": int(getattr(resp, "failedFiles", 0) or 0),
            "error_message": getattr(resp, "errorMessage", "") or "",
        }

    async def restart_backup_walkthrough(self, source_path: str) -> bool:
        """``BackupRestartWalkingThrough`` —— 让备份重新扫描源目录。

        这是“取消上传后重新上传”最正统的做法：备份重新遍历源目录，
        发现目标端缺失的文件就会重新上传，无需手工构造上传任务。
        """
        pb2, _ = _ProtoHolder.load()
        await self._call(
            "BackupRestartWalkingThrough", pb2.StringValue(value=source_path)
        )
        logger.info("已触发备份重新扫描: %s", source_path)
        return True

    # ------------------------------------------------------ 重新触发上传
    async def resolve_backup_for_path(
        self, source_path: str
    ) -> tuple[str, str] | None:
        """找出 ``source_path`` 所属的备份任务。

        :return: ``(备份源目录, 命中的目标目录)``；找不到返回 None。
                 命中的目标目录用于校验“该备份确实往我们关心的目标写过”。
        """
        if not source_path:
            return None

        try:
            backups = await self.list_backups()
        except CD2Error as exc:
            logger.warning("列出备份任务失败: %s", exc)
            return None

        normalized = source_path.replace("\\", "/")
        best: tuple[str, str] | None = None
        best_len = -1

        for backup in backups:
            raw_src = (backup.get("source_path") or "").replace("\\", "/")
            # 保留原始值以区分“未配置”与“根目录 /”
            if not raw_src:
                continue
            src = raw_src.rstrip("/")
            # 源路径必须位于备份源目录之下（取最长匹配，避免父目录抢走子备份）。
            # 备份源为根目录时 src 为空串，此时任何绝对路径都算其下。
            prefix = f"{src}/" if src else "/"
            if normalized != src and not normalized.startswith(prefix):
                continue
            enabled_dests = [
                d["path"]
                for d in backup.get("destinations", [])
                if d.get("enabled") and d.get("path")
            ]
            if not enabled_dests:
                continue
            if len(src) > best_len:
                # 返回原始路径（根目录场景下即为 "/"，便于回传给 RPC）
                best = (raw_src, enabled_dests[0])
                best_len = len(src)

        return best

    async def retrigger_upload(self, source_path: str, dest_path: str) -> bool:
        """重新触发“源文件上传”（用于 cancel_reupload 策略）。

        本项目通过 **CD2 备份任务** 产生上传，因此正确做法是：

        1. ``BackupGetAll`` 找到 ``source_path`` 所属的备份任务；
        2. ``BackupRestartWalkingThrough(备份源目录)`` 让它重新扫描；
           备份会重新比对目标端，缺失文件即重新上传。

        若找不到所属备份（例如用户用的是拷贝任务或挂载写入），
        退化为 ``CopyFile`` 尝试；仍失败则返回 False，由调度器标记最终失败。
        """
        pb2, _ = _ProtoHolder.load()

        if not source_path:
            logger.warning("retrigger_upload 缺少源路径，跳过")
            return False

        # ---- 主路径：通过备份任务重新扫描 ----
        resolved = await self.resolve_backup_for_path(source_path)
        if resolved is not None:
            backup_source, _target = resolved
            try:
                await self.restart_backup_walkthrough(backup_source)
                log_event(
                    "info",
                    "cd2",
                    "已通过备份重新扫描触发重传",
                    detail=f"file={source_path} backup={backup_source}",
                )
                return True
            except CD2Error as exc:
                logger.warning(
                    "BackupRestartWalkingThrough 失败（将从 %s 回退）: %s",
                    backup_source, exc,
                )
        else:
            logger.info(
                "未找到 %s 所属的备份任务，尝试 CopyFile 回退", source_path
            )

        # ---- 回退：CopyFile ----
        if not dest_path:
            return False
        parent = dest_path.rstrip("/").rsplit("/", 1)[0] or "/"
        try:
            request = pb2.CopyFileRequest(
                theFilePaths=[source_path],
                destPath=parent,
                conflictPolicy=2,  # Skip：目标已存在时跳过，避免重复
            )
            resp = await self._call("CopyFile", request)
            if bool(getattr(resp, "success", False)):
                logger.info(
                    "retrigger_upload 通过 CopyFile 成功: %s → %s",
                    source_path, parent,
                )
                return True
            logger.warning(
                "CopyFile 未成功: %s",
                getattr(resp, "errorMessage", "") or "unknown",
            )
        except CD2Error as exc:
            logger.warning("retrigger_upload CopyFile 失败: %s", exc)

        log_event(
            "warning",
            "cd2",
            "无法重新触发上传，已放弃该任务",
            detail=f"source={source_path} dest={dest_path}",
        )
        return False


# ===========================================================================
# 全局单例
# ===========================================================================
_client: CD2Client | None = None


def get_cd2_client() -> CD2Client:
    """获取进程内唯一的 CD2 客户端。"""
    global _client
    if _client is None:
        _client = CD2Client()
    return _client
