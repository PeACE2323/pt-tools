"""115 网盘客户端封装（基于 p115client）。

严格遵循 p115client 的接口规范：

* ``P115Client(cookies)`` 用 Cookie 初始化
* 所有调用走 ``await client.xxx(payload, async_=True)``
* 用 ``check_response`` 校验响应（state 为假时抛 P115OSError）
* ``client.request(...)`` 作为底层扩展调用

异步请求实现：``async_=True`` 需要一个能接收 ``async_=`` 的 HTTP 封装，
本项目使用 ``httpx_request.request``；若该包不可用则回退到
``urllib3_future_request.request``（其 ``async_`` 参数支持可能受限，
届时自动降级为线程池执行同步调用，功能不受影响）。

**离线任务删除防坑**（需求重点）：

* 优先使用封装方法（若当前 p115client 版本提供 ``clouddownload_task_del``）
* 失败则回退到 ``client.request`` 直连 ``?ac=task_del``
* **表单必须用单任务格式** ``{"hash[0]": info_hash, "flag": 0}``，
  逐个删除；绝不提交 ``hash[]`` 数组，避免静默失败
* 只移除离线任务，不删除已落盘的源文件
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..config import (
    COOKIE_FILE,
    P115_CLOUDDOWNLOAD_HOST,
    P115_FAILED_STATUS,
    P115_LIXIAN_HOST,
    P115_ST_CANCELLED,
    P115_ST_DOWNLOADING,
    P115_ST_FAILED,
    P115_ST_FINISHED,
    P115_ST_REMOVED,
    P115_STAT_ALL,
)
from ..utils import get_logger, log_event, utcnow_ts

logger = get_logger("guard.p115")


# ===========================================================================
# 异常
# ===========================================================================
class P115Error(Exception):
    """115 相关异常基类。"""


class P115NotConfigured(P115Error):
    """未配置 Cookie。"""


class P115AuthExpired(P115Error):
    """Cookie 失效 / 需要重新登录。"""


class P115LibMissing(P115Error):
    """p115client 未安装或导入失败。"""


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass(slots=True)
class OfflineTask:
    """115 云下载任务的规范化视图。"""

    info_hash: str
    name: str
    size: int
    add_time: int
    status: int
    percent_done: float
    url: str = ""
    del_path: str = ""
    file_id: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized_status(self) -> str:
        """归一化状态。

        **判据只有进度，status 仅用于识别明确的失败。**

        115 的 ``status`` 数值语义不可靠：社区资料互相矛盾，实测也踩过坑
        —— 曾把 ``status=1`` 当作「已移除」，结果一个进度 99% 的正常下载
        任务被判成已移除，超时监控完全失效。

        现在对齐经实测验证的判据（与成熟的清理脚本一致）：

        1. ``status`` 命中失败集合 → failed
        2. ``percentDone >= 100``  → finished（**99% 仍属未完成**）
        3. 其余一律视为进行中（含 0% 的刚入队任务）

        注意第 2 条：必须用 ``>= 100`` 而不是 ``>= 99.9``。
        115 的进度是整数百分比，99% 表示还差最后一点，仍然要参与超时判定。
        """
        if self.status in P115_FAILED_STATUS:
            return P115_ST_FAILED
        if self.percent_done >= 100.0:
            return P115_ST_FINISHED
        # 只要进度没到 100，就属于「未完成」——包括 0%。
        # 这类任务都要参与超时判定，也是用户最关心要清理的对象。
        return P115_ST_DOWNLOADING

    @property
    def is_finished(self) -> bool:
        return self.normalized_status == P115_ST_FINISHED

    @property
    def is_terminal(self) -> bool:
        """已结束态（完成/失败/已删除）——不参与超时清理。"""
        return self.normalized_status in (
            P115_ST_FINISHED,
            P115_ST_FAILED,
            P115_ST_REMOVED,
            P115_ST_CANCELLED,
        )

    def age_seconds(self, now: float | None = None) -> float:
        """任务已存在时长（秒）：now - add_time。"""
        current = utcnow_ts() if now is None else now
        if self.add_time <= 0:
            return 0.0
        return max(0.0, current - self.add_time)


@dataclass(slots=True)
class P115Status:
    """115 登录状态快照。"""

    configured: bool = False
    logged_in: bool = False
    user_id: int = 0
    message: str = ""
    last_error: str = ""
    task_count: int = 0
    checked_at: float = field(default_factory=utcnow_ts)


# ===========================================================================
# 请求器（异步）解析
# ===========================================================================
def _resolve_request_impl() -> Any:
    """返回一个能接受 ``async_=`` 关键字的 HTTP 请求函数。

    优先级：httpx_request（推荐，原生异步）→ urllib3_future_request → None
    """
    try:
        from httpx_request import request as httpx_request  # type: ignore

        return httpx_request
    except Exception as exc:  # noqa: BLE001
        logger.debug("httpx_request 不可用: %s", exc)

    try:
        from urllib3_future_request import request as urllib3_request  # type: ignore

        return urllib3_request
    except Exception as exc:  # noqa: BLE001
        logger.debug("urllib3_future_request 不可用: %s", exc)

    return None


# ===========================================================================
# 客户端
# ===========================================================================
class P115ClientWrapper:
    """115 网盘异步封装。"""

    def __init__(self) -> None:
        self._cookie: str = ""
        self._client: Any = None
        self._request_impl: Any = None
        self._lock = asyncio.Lock()
        self._status = P115Status()
        self._async_ok = True  # 能否使用真正的 async_=True

    # ---------------------------------------------------------------- 配置
    @property
    def status(self) -> P115Status:
        return self._status

    @property
    def configured(self) -> bool:
        return bool(self._cookie)

    async def configure(self, cookie: str) -> None:
        """设置 Cookie；内容变化时重建 client 并落盘。"""
        cookie = (cookie or "").strip()
        if cookie == self._cookie and self._client is not None:
            return
        await self.close()
        self._cookie = cookie
        self._status = P115Status(configured=bool(cookie))

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        # p115client 的 client 本身没有必须关闭的连接（每次请求独立），
        # 但若其内部持有 session，尽力关闭。
        for attr in ("close", "aclose"):
            closer = getattr(client, attr, None)
            if callable(closer):
                try:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.debug("关闭 115 client 异常（忽略）: %s", exc)
                break

    # ------------------------------------------------------------ Cookie IO
    @staticmethod
    def _persist_cookie(cookie: str) -> None:
        """把 Cookie 持久化到 DATA_DIR/115-cookies.txt（权限 600）。"""
        try:
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_FILE.write_text(cookie, encoding="utf-8")
            try:
                COOKIE_FILE.chmod(0o600)
            except Exception:  # noqa: BLE001 - Windows 等平台可能不支持
                pass
            logger.info("115 Cookie 已保存到 %s", COOKIE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存 115 Cookie 文件失败: %s", exc)

    @staticmethod
    def load_cookie_from_file() -> str:
        """从文件读取上次保存的 Cookie。"""
        try:
            if COOKIE_FILE.exists():
                text = COOKIE_FILE.read_text(encoding="utf-8").strip()
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 115 Cookie 文件失败: %s", exc)
        return ""

    # ------------------------------------------------------------ 客户端构建
    def _build_client(self) -> Any:
        try:
            from p115client import P115Client  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise P115LibMissing(
                f"p115client 导入失败，请执行 pip install -U p115client（{exc}）"
            ) from exc

        if not self._cookie:
            raise P115NotConfigured("尚未配置 115 Cookie")

        if self._request_impl is None:
            self._request_impl = _resolve_request_impl()
            if self._request_impl is None:
                logger.warning(
                    "未找到 httpx_request / urllib3_future_request，"
                    "异步调用将降级为线程池执行同步请求"
                )

        client = P115Client(self._cookie)
        return client

    async def ensure_client(self) -> Any:
        """惰性构建并返回底层 P115Client。"""
        async with self._lock:
            if self._client is not None:
                return self._client
            self._client = self._build_client()
            return self._client

    # ------------------------------------------------------------ 调用封装
    async def _call(
        self,
        method_name: str,
        *args: Any,
        payload: Any = None,
        **kwargs: Any,
    ) -> dict:
        """统一调用：async_=True → check_response → 异常归类。

        ``method_name`` 是 P115Client 的方法名，例如 ``clouddownload_task_list``。
        """
        client = await self.ensure_client()

        method = getattr(client, method_name, None)
        if method is None:
            raise P115Error(
                f"当前 p115client 版本不提供方法 {method_name}，"
                "请升级：pip install -U p115client"
            )

        try:
            from p115client import check_response  # type: ignore
        except Exception:  # noqa: BLE001
            check_response = None  # type: ignore

        call_kwargs = dict(kwargs)
        if self._request_impl is not None:
            call_kwargs.setdefault("request", self._request_impl)

        resp: Any = None
        try:
            if self._async_ok and self._request_impl is not None:
                try:
                    resp = method(*args, async_=True, **call_kwargs)
                    if asyncio.iscoroutine(resp) or hasattr(resp, "__await__"):
                        resp = await resp
                except (TypeError, ValueError) as exc:
                    # 某些请求器不接受 async_=True，降级为线程池同步调用
                    logger.warning(
                        "异步调用 %s 失败，降级为同步线程池执行: %s",
                        method_name, exc,
                    )
                    self._async_ok = False
                    resp = await asyncio.to_thread(
                        method, *args, **(kwargs if payload is None else kwargs)
                    )
            else:
                resp = await asyncio.to_thread(method, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            raise self._classify(exc, method_name) from exc

        # ---- 响应校验 ----
        if isinstance(resp, dict) and check_response is not None:
            try:
                check_response(resp)
            except BaseException as exc:  # noqa: BLE001
                raise self._classify(exc, method_name) from exc
        return resp if isinstance(resp, dict) else {"data": resp}

    @staticmethod
    def _classify(exc: BaseException, method_name: str) -> P115Error:
        """把 p115client 异常归类成我们自己的异常类型。"""
        name = exc.__class__.__name__
        text = f"{name}: {exc}"
        args = getattr(exc, "args", ())

        # p115client 的错误对象形如 OSError(errno, payload)
        errno_val = args[0] if args and isinstance(args[0], int) else None
        payload = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
        errno_field = payload.get("errno") if isinstance(payload, dict) else None

        auth_markers = ("LoginError", "Authentication", "EAUTH")
        if (
            any(m in name for m in auth_markers)
            or errno_field in (99, 911, 990001, 40101017, 40101032)
            or errno_val == 13
        ):
            return P115AuthExpired(
                f"115 Cookie 已失效或需要验证，请在系统设置中更新 Cookie（{text}）"
            )

        return P115Error(f"调用 {method_name} 失败: {text}")

    # ------------------------------------------------------------ 状态更新
    def mark_status(
        self,
        *,
        logged_in: bool | None = None,
        user_id: int | None = None,
        message: str | None = None,
        last_error: str | None = None,
        task_count: int | None = None,
    ) -> None:
        """由定时健康检查更新状态快照。

        仪表盘读的是 ``status.logged_in``，原先只在用户手动点「连接测试」
        时更新，导致重启后一直显示「未验证」。
        """
        if logged_in is not None:
            self._status.logged_in = logged_in
        if user_id is not None:
            self._status.user_id = user_id
        if message is not None:
            self._status.message = message
        if last_error is not None:
            self._status.last_error = last_error
        if task_count is not None:
            self._status.task_count = task_count
        self._status.configured = self.configured
        self._status.checked_at = utcnow_ts()

    # ------------------------------------------------------------ 登录检查
    async def check_login(self) -> dict[str, Any]:
        """通过 ``fs_files``（根目录第一页）验证 Cookie 是否有效。"""
        result: dict[str, Any] = {"ok": False, "message": ""}
        # 注意：不能整体重置 _status，否则会丢掉 task_count 等已有信息。
        # 只更新与「登录有效性」相关的字段。
        self._status.configured = self.configured

        if not self.configured:
            result["message"] = "未配置 115 Cookie"
            return result

        try:
            resp = await self._call(
                "fs_files", {"cid": 0, "limit": 1, "offset": 0, "show_dir": 1}
            )
            user_id = 0
            try:
                client = await self.ensure_client()
                user_id = int(getattr(client, "user_id", 0) or 0)
            except Exception:  # noqa: BLE001
                pass

            count = len(resp.get("data") or [])
            self._status.logged_in = True
            self._status.user_id = user_id
            self._status.message = f"已登录（根目录样本 {count} 项）"
            result.update(
                {"ok": True, "user_id": user_id, "message": self._status.message}
            )
            return result

        except P115AuthExpired as exc:
            self._status.logged_in = False
            self._status.last_error = str(exc)
            result["message"] = str(exc)
            result["expired"] = True
            log_event("warning", "p115", "115 Cookie 已失效", detail=str(exc))
            return result
        except P115Error as exc:
            self._status.logged_in = False
            self._status.last_error = str(exc)
            result["message"] = str(exc)
            return result
        finally:
            self._status.checked_at = utcnow_ts()

    # ------------------------------------------------------------ 云下载列表
    @staticmethod
    def _parse_task(raw: dict[str, Any]) -> OfflineTask | None:
        """把 115 原始任务解析成 ``OfflineTask``。"""
        info_hash = str(raw.get("info_hash") or "").strip().lower()
        if not info_hash:
            return None

        def _to_int(value: Any, default: int = 0) -> int:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        def _to_float(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        percent = _to_float(raw.get("percentDone", 0))
        # 部分响应里 percentDone 是 0~1 的小数
        if 0 < percent <= 1:
            percent *= 100.0

        return OfflineTask(
            info_hash=info_hash,
            name=str(raw.get("name") or "").strip(),
            size=_to_int(raw.get("size")),
            add_time=_to_int(raw.get("add_time")),
            status=_to_int(raw.get("status")),
            percent_done=max(0.0, min(100.0, percent)),
            url=str(raw.get("url") or ""),
            del_path=str(raw.get("del_path") or ""),
            file_id=_to_int(raw.get("file_id")),
            raw=raw,
        )

    async def list_offline_tasks(
        self,
        page_size: int = 30,
        max_pages: int = 50,
        stat: int = P115_STAT_ALL,
    ) -> list[OfflineTask]:
        """拉取云下载任务列表（自动翻页）。

        优先用封装方法 ``clouddownload_task_list``；不可用时回退到
        ``client.request`` 直连 ``?ac=task_lists``。

        注意：p115client 的 ``clouddownload_iter`` 里 page 从 0 开始，
        这里保持一致。
        """
        await self.ensure_client()
        tasks: list[OfflineTask] = []
        seen: set[str] = set()

        page = 0
        while page < max_pages:
            payload = {"page": page, "page_size": page_size, "stat": stat}
            try:
                resp = await self._call("clouddownload_task_list", payload)
            except P115Error as exc:
                # 方法不存在时回退到底层 request
                if "不提供方法" in str(exc):
                    logger.info("回退到 client.request 直连 task_lists")
                    resp = await self._request_task_lists(page, page_size)
                else:
                    raise

            raw_tasks = resp.get("tasks")
            if raw_tasks is None:
                # 有些版本把数据放在 data 下
                data = resp.get("data") or {}
                raw_tasks = data.get("tasks") if isinstance(data, dict) else None
            raw_tasks = raw_tasks or []

            for raw in raw_tasks:
                if not isinstance(raw, dict):
                    continue
                task = self._parse_task(raw)
                if task is None or task.info_hash in seen:
                    continue
                seen.add(task.info_hash)
                tasks.append(task)

            page_count = resp.get("page_count")
            current = resp.get("page", page)
            if not raw_tasks:
                break
            if page_count is not None and current is not None:
                if int(current) >= int(page_count) - 1:
                    break
            page += 1

        self._status.task_count = len(tasks)
        logger.debug("115 云下载任务 %d 条", len(tasks))
        return tasks

    async def _request_task_lists(self, page: int, page_size: int) -> dict:
        """底层回退：``client.request`` 请求 task_lists。"""
        client = await self.ensure_client()
        url = f"{P115_CLOUDDOWNLOAD_HOST}/?ac=task_lists"

        def _do() -> dict:
            return client.request(
                url=url,
                method="GET",
                params={"page": page, "page_size": page_size},
            )

        try:
            return await asyncio.to_thread(_do)
        except BaseException as exc:  # noqa: BLE001
            raise self._classify(exc, "task_lists") from exc

    # ------------------------------------------------------------ 删除任务
    async def remove_offline_task(self, info_hash: str) -> tuple[bool, str]:
        """删除单个云下载任务（不删除已下载的源文件）。

        实现顺序（防坑）：

        1. 尝试封装方法 ``clouddownload_task_del`` / ``clouddownload_task_delete``
        2. 回退到 ``client.request`` 直连，表单用**单任务格式**
           ``{"hash[0]": info_hash, "flag": 0}``
        3. 逐个删除，绝不批量提交数组

        :return: (是否成功, 说明)
        """
        info_hash = (info_hash or "").strip().lower()
        if not info_hash:
            return False, "info_hash 为空"

        client = await self.ensure_client()

        # ---- 1) 封装方法 ----
        for method_name in ("clouddownload_task_del", "clouddownload_task_delete"):
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                resp = method(info_hash, async_=self._async_ok and self._request_impl is not None,
                              **({"request": self._request_impl} if self._request_impl else {}))
                if hasattr(resp, "__await__"):
                    resp = await resp
                try:
                    from p115client import check_response  # type: ignore

                    check_response(resp)
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(resp, dict) and resp.get("state", True):
                    logger.info(
                        "已通过 %s 删除云下载任务 %s", method_name, info_hash
                    )
                    return True, "ok"
                logger.warning(
                    "%s 返回失败，继续尝试底层请求: %s", method_name, resp
                )
            except BaseException as exc:  # noqa: BLE001
                logger.warning("%s 调用异常: %s", method_name, exc)

        # ---- 2) 底层单任务表单 ----
        urls = [
            f"{P115_CLOUDDOWNLOAD_HOST}/?ac=task_del",
            f"{P115_LIXIAN_HOST}/lixian/?ct=lixian&ac=task_del",
        ]
        last_error = ""
        for url in urls:
            # 关键：单任务格式 hash[0]，逐个删除
            data = {"hash[0]": info_hash, "flag": 0}

            def _do(target_url: str = url, form: dict = data) -> dict:
                return client.request(
                    url=target_url,
                    method="POST",
                    data=form,
                )

            try:
                resp = await asyncio.to_thread(_do)
            except BaseException as exc:  # noqa: BLE001
                last_error = f"{url} 请求异常: {exc}"
                logger.warning(last_error)
                continue

            if not isinstance(resp, dict):
                last_error = f"{url} 响应非 JSON: {resp!r}"
                continue

            state = resp.get("state")
            if state in (True, 1):
                logger.info("已通过 task_del 删除云下载任务 %s", info_hash)
                return True, "ok"

            last_error = (
                f"{url} 删除失败: state={state} "
                f"error={resp.get('error') or resp.get('msg') or ''} "
                f"errno={resp.get('errno')}"
            )
            # Cookie 失效直接抛出，不必再试下一个域名
            if resp.get("errno") in (99, 911, 990001):
                raise P115AuthExpired(last_error)

        logger.error("删除云下载任务 %s 最终失败: %s", info_hash, last_error)
        return False, last_error

    async def remove_offline_tasks(
        self, info_hashes: list[str]
    ) -> dict[str, tuple[bool, str]]:
        """逐个删除多个任务（保持单条表单，避免静默失败）。"""
        results: dict[str, tuple[bool, str]] = {}
        for info_hash in info_hashes:
            try:
                results[info_hash] = await self.remove_offline_task(info_hash)
            except P115Error as exc:
                results[info_hash] = (False, str(exc))
            # 轻微间隔，避免触发风控
            await asyncio.sleep(0.3)
        return results

    # ------------------------------------------------------------ 重试任务
    async def restart_offline_task(self, info_hash: str) -> tuple[bool, str]:
        """重启失败的云下载任务（``clouddownload_task_restart``）。"""
        try:
            resp = await self._call("clouddownload_task_restart", info_hash)
            return True, str(resp.get("state", "ok"))
        except P115Error as exc:
            return False, str(exc)


# ===========================================================================
# 全局单例
# ===========================================================================
_wrapper: P115ClientWrapper | None = None


def get_p115_client() -> P115ClientWrapper:
    """获取进程内唯一的 115 客户端封装。"""
    global _wrapper
    if _wrapper is None:
        _wrapper = P115ClientWrapper()
    return _wrapper
