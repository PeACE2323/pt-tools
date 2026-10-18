"""BT 流量监控子服务（bt-traffic-guard）的进程管理与反向代理。

## 为什么是「子进程 + 反代」而不是重写

bt-traffic-guard 是一个独立的 Go 服务（自带 HTTP 服务器与单页前端），
负责监控 qBittorrent / Transmission 的上传流量并按策略自动限速。
它的策略引擎（661 行）与前端（1707 行）逻辑已经很成熟。

本项目（Python + FastAPI）与它语言不同、都有 ``/api/*`` 前缀。
整合方式是：

    PTtools(8899)  ──/btg/*──▶  反向代理  ──▶  bt-traffic-guard(127.0.0.1:8080)

这样：
  * 单容器、单端口（8899），用户只需访问一个地址
  * Go 侧代码零改动，功能 100% 保留
  * 两个服务生命周期由 Python 统一管理
  * 任一侧崩溃都不会拖垮另一侧

## 关键设计

1. **只监听 127.0.0.1**：Go 服务不对外暴露，只能通过本项目的反代访问，
   避免出现「绕过 PTtools 直连 8080」的口子。
2. **路径重写**：``/btg/api/status`` → ``/api/status``，
   同时处理 Go 侧前端里写死的 ``/api/`` 绝对路径（见 ``rewrite_html``）。
3. **自动重启**：子进程异常退出后按退避重启，连续失败过多则停止并记录。
4. **优雅关闭**：容器停止时先发 SIGTERM，超时再 SIGKILL，
   确保 Go 侧有机会「恢复上传限速」——这是它退出时的既定行为，
   否则下载器会被永久留在限速状态。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR
from ..utils import get_logger, log_event, utcnow_ts

logger = get_logger("guard.btg")


# ===========================================================================
# 常量
# ===========================================================================
#: Go 二进制在镜像中的位置（Dockerfile 多阶段构建产出）
BTG_BINARY = os.environ.get("BTG_BINARY", "/app/btguard-bin/bt-traffic-guard")

#: 子进程监听地址。强制 127.0.0.1，禁止外部直连。
BTG_LISTEN = "127.0.0.1:18080"

#: 配置与数据目录（放在 /app/data 下，随卷持久化）
BTG_CONFIG_DIR = DATA_DIR / "btguard" / "config"
BTG_DATA_DIR = DATA_DIR / "btguard" / "data"

#: 反代使用的路径前缀
PROXY_PREFIX = "/btg"

#: 需要重写的请求/响应头（避免 Host 与实际端口不匹配）
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

#: 重启退避（秒）
_RESTART_BACKOFF = [1, 2, 5, 10, 30]


# ===========================================================================
# 状态
# ===========================================================================
@dataclass
class BTGStatus:
    """BT 子服务的运行状态快照，供仪表盘展示。"""

    available: bool = False          # 二进制是否存在
    running: bool = False            # 进程是否在运行
    healthy: bool = False            # 健康检查是否通过
    managed: bool = False            # 是否由本进程托管
    pid: int = 0
    listen: str = BTG_LISTEN
    version: str = ""
    last_error: str = ""
    started_at: float = 0.0
    restart_count: int = 0
    checked_at: float = field(default_factory=utcnow_ts)


# ===========================================================================
# 管理器
# ===========================================================================
class BTGuardManager:
    """bt-traffic-guard 子进程的生命周期管理。"""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._enabled = True
        self._restart_index = 0
        self._status = BTGStatus()
        self._http: object | None = None  # httpx.AsyncClient（延迟导入）

    # ------------------------------------------------------------ 基础信息
    @property
    def status(self) -> BTGStatus:
        return self._status

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        """启用/禁用托管（禁用后不会自动启动）。"""
        self._enabled = bool(value)

    @property
    def binary_path(self) -> Path:
        return Path(BTG_BINARY)

    def is_available(self) -> bool:
        """二进制是否可用（存在且可执行）。"""
        p = self.binary_path
        try:
            return p.is_file() and os.access(p, os.X_OK)
        except OSError:
            return False

    # ------------------------------------------------------------ HTTP 客户端
    def _client(self):
        """返回指向子服务的 httpx 客户端（延迟导入）。

        httpx 在这里延迟导入，是为了让本模块可以在没有 httpx 的环境里
        被导入（单元测试、静态检查），不影响运行时行为。
        """
        import httpx

        if self._http is None or getattr(self._http, "is_closed", True):
            self._http = httpx.AsyncClient(
                base_url=f"http://{BTG_LISTEN}",
                timeout=httpx.Timeout(10.0, connect=3.0),
                follow_redirects=False,
            )
        return self._http

    # ------------------------------------------------------------ 启动
    async def start(self) -> bool:
        """启动子进程。返回是否成功拉起。

        二进制不存在时**不算错误**：说明镜像构建时未包含 Go 产物，
        此时 BT 页签会显示「未安装」，其余功能不受影响。
        """
        if self._proc is not None and self._proc.returncode is None:
            return True

        if not self.is_available():
            self._status.available = False
            self._status.running = False
            self._status.last_error = (
                f"未找到可执行文件 {BTG_BINARY}"
                "（镜像构建时可能未包含 Go 产物，BT 功能不可用，其余功能正常）"
            )
            logger.warning("BT 子服务不可用: %s", self._status.last_error)
            return False

        self._status.available = True
        self._prepare_dirs()

        cmd = [
            str(self.binary_path),
            "-config", str(BTG_CONFIG_DIR / "config.yaml"),
        ]
        env = os.environ.copy()
        # 让 Go 侧监听内部地址
        env["BTG_LISTEN"] = BTG_LISTEN
        env.setdefault("TZ", os.environ.get("TZ", "Asia/Shanghai"))

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(DATA_DIR),
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            self._status.running = False
            self._status.last_error = f"启动失败: {exc}"
            logger.exception("BT 子服务启动失败")
            log_event("error", "btguard", "BT 子服务启动失败", detail=str(exc))
            return False

        self._status.running = True
        self._status.pid = self._proc.pid or 0
        self._status.started_at = utcnow_ts()
        self._status.last_error = ""
        logger.info(
            "BT 子服务已启动: pid=%s listen=%s", self._proc.pid, BTG_LISTEN
        )

        # 持续转发子进程输出到本项目日志，便于统一排查
        self._reader_task = asyncio.create_task(
            self._pump_logs(), name="btg-logs"
        )
        return True

    def _prepare_dirs(self) -> None:
        """准备配置/数据目录，并在首次运行时铺一份默认配置。"""
        BTG_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        BTG_DATA_DIR.mkdir(parents=True, exist_ok=True)

        cfg = BTG_CONFIG_DIR / "config.yaml"
        if cfg.exists():
            return

        # 优先从镜像里的示例配置复制，否则写一份最小可用配置
        example = Path("/app/btguard/config.example.yaml")
        if example.is_file():
            try:
                shutil.copyfile(example, cfg)
                logger.info("已用示例配置初始化 BT 配置: %s", cfg)
                return
            except OSError as exc:  # noqa: BLE001
                logger.warning("复制示例配置失败: %s", exc)

        cfg.write_text(
            "# PTtools 自动生成的初始配置\n"
            f"listen: \"{BTG_LISTEN}\"\n"
            "clients: []\n",
            encoding="utf-8",
        )
        logger.info("已生成默认 BT 配置: %s", cfg)

    async def _pump_logs(self) -> None:
        """把子进程 stdout/stderr 转发到本项目日志。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info("[btg] %s", text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("BT 日志转发结束: %s", exc)

    # ------------------------------------------------------------ 停止
    async def stop(self, timeout: float = 8.0) -> None:
        """停止子进程。

        先 SIGTERM 让 Go 侧执行收尾（**恢复上传限速**），超时才 SIGKILL。
        这一步很关键：直接 kill 会让下载器永久停留在限速状态。
        """
        self._stopping = True

        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            self._status.running = False
            return

        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            self._proc = None
            self._status.running = False
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送 SIGTERM 失败: %s", exc)

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            logger.info("BT 子服务已优雅退出")
        except asyncio.TimeoutError:
            logger.warning("BT 子服务未在 %.0fs 内退出，强制结束", timeout)
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:  # noqa: BLE001
                pass

        self._proc = None
        self._status.running = False
        self._status.healthy = False
        self._stopping = False

    # ------------------------------------------------------------ 健康检查
    async def health(self) -> bool:
        """探测子服务是否可响应。"""
        if self._proc is None or self._proc.returncode is not None:
            self._status.healthy = False
            return False
        try:
            resp = await self._client().get("/api/healthz")
            ok = resp.status_code < 500
        except Exception:  # noqa: BLE001
            ok = False
        self._status.healthy = ok
        self._status.checked_at = utcnow_ts()
        return ok

    async def probe_and_maybe_restart(self) -> None:
        """由定时任务调用：检查存活，异常则按退避重启。"""
        if not self._enabled or self._stopping:
            return

        proc = self._proc
        if proc is None:
            if not self.is_available():
                return
            await self._restart("进程未运行")
            return

        if proc.returncode is not None:
            await self._restart(f"进程退出 code={proc.returncode}")
            return

        if not await self.health():
            # 首次启动可能尚在初始化，这里不立刻重启，只记录
            logger.debug("BT 子服务健康检查未通过（可能需要预热）")

    async def _restart(self, reason: str) -> None:
        """按退避策略重启。"""
        delay = _RESTART_BACKOFF[min(self._restart_index, len(_RESTART_BACKOFF) - 1)]
        self._restart_index += 1
        self._status.restart_count += 1

        logger.warning("BT 子服务重启（%s），%ss 后重试", reason, delay)
        if self._restart_index >= len(_RESTART_BACKOFF):
            log_event(
                "error",
                "btguard",
                "BT 子服务多次重启失败，已停止自动重启",
                detail=reason,
            )
            self._status.last_error = f"多次重启失败: {reason}"
            self._enabled = False
            return

        await asyncio.sleep(delay)
        if self._stopping:
            return
        await self.start()

    def reset_restart_counter(self) -> None:
        """健康后重置退避计数。"""
        self._restart_index = 0

    # ------------------------------------------------------------ 数据访问
    #
    # 下面这些方法让 PTtools 用**自己的界面**渲染 BT 的数据，
    # 而不是把 Go 的整页 iframe 套进来。这样两边的观感、主题、
    # 交互才是一致的，也才能真正叫「整合」。

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        """向子服务取 JSON。失败抛 ``_proxy_unavailable``。"""
        import httpx

        try:
            resp = await self._client().get(path, params=params)
        except httpx.HTTPError as exc:
            raise _proxy_unavailable(str(exc)) from exc
        if resp.status_code >= 400:
            raise _proxy_unavailable(
                f"{path} 返回 {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise _proxy_unavailable(f"{path} 返回的不是 JSON") from exc
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_status(self) -> dict:
        """``/api/status``：实时速率、客户端明细、策略进度。"""
        return await self._get_json("/api/status")

    async def fetch_events(self, limit: int = 100) -> list[dict]:
        """``/api/events``：策略触发/解除等事件。"""
        data = await self._get_json("/api/events", {"limit": limit})
        events = data.get("events")
        return events if isinstance(events, list) else []

    async def fetch_history(self, client: str = "") -> list[dict]:
        """``/api/history``：采样曲线。``client`` 为空表示聚合曲线。"""
        data = await self._get_json("/api/history", {"client": client})
        samples = data.get("samples")
        return samples if isinstance(samples, list) else []

    async def fetch_config(self) -> dict:
        """``/api/config``：脱敏后的结构化配置。"""
        return await self._get_json("/api/config")

    async def fetch_config_yaml(self) -> str:
        """``/api/config/raw``：原始 YAML 文本。

        写配置时用 YAML 而不是逐字段映射：Go 侧的配置结构字段很多
        （客户端、策略、通知、鉴权…），逐个映射既易漏又容易随版本漂移，
        直接读写 YAML 能保证语义完整。
        """
        import httpx

        try:
            resp = await self._client().get("/api/config/raw")
        except httpx.HTTPError as exc:
            raise _proxy_unavailable(str(exc)) from exc
        if resp.status_code >= 400:
            raise _proxy_unavailable(
                f"/api/config/raw 返回 {resp.status_code}"
            )
        return resp.text

    async def save_config_yaml(self, yaml_text: str) -> dict:
        """``POST /api/config/raw``：保存 YAML 并热重载。"""
        import httpx

        try:
            resp = await self._client().post(
                "/api/config/raw", json={"yaml": yaml_text}
            )
        except httpx.HTTPError as exc:
            raise _proxy_unavailable(str(exc)) from exc
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                payload = resp.json()
                detail = payload.get("error") or payload.get("message") or detail
            except ValueError:
                pass
            raise _proxy_unavailable(f"保存失败: {detail}")
        try:
            return resp.json()
        except ValueError:
            return {"ok": True}

    async def policy_action(self, action: str, name: str) -> dict:
        """策略操作：``resume``（解除限速）或 ``trigger``（手动触发）。"""
        import httpx

        if action not in ("resume", "trigger"):
            raise ValueError(f"不支持的操作: {action}")
        try:
            resp = await self._client().post(
                f"/api/policy/{action}", params={"name": name}
            )
        except httpx.HTTPError as exc:
            raise _proxy_unavailable(str(exc)) from exc
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                payload = resp.json()
                detail = payload.get("error") or payload.get("message") or detail
            except ValueError:
                pass
            raise _proxy_unavailable(detail)
        try:
            return resp.json()
        except ValueError:
            return {"ok": True}

    async def test_client(self, name: str) -> dict:
        """测试某个下载器的连通性。"""
        import httpx

        try:
            resp = await self._client().get(
                "/api/client/test", params={"name": name}
            )
        except httpx.HTTPError as exc:
            raise _proxy_unavailable(str(exc)) from exc
        try:
            return resp.json()
        except ValueError:
            return {"ok": resp.status_code < 400}

    # ------------------------------------------------------------ 关闭
    async def close(self) -> None:
        await self.stop()
        if self._http is not None and not self._http.is_closed:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._http = None


# ===========================================================================
# 反向代理（供 FastAPI 路由使用）
# ===========================================================================
def rewrite_html(html: str, prefix: str = PROXY_PREFIX) -> str:
    """把 Go 前端里写死的绝对路径改成带前缀的路径。

    Go 侧的单页应用直接请求 ``/api/status`` 这类绝对路径。
    经由 ``/btg/`` 反代访问时，这些请求会打到本项目的根作用域，
    必须改写成 ``/btg/api/status``。

    只改写出现在属性/字符串里的 ``"/api/...`` 与 ``'/api/...``，
    避免误伤正文中的普通文本。
    """
    import re

    # 双引号 / 单引号 / 反引号 开头的 /api/
    html = re.sub(r'(["\'])(' + re.escape(prefix) + r')?/api/', r"\1" + prefix + "/api/", html)
    # fetch('/api/...')  之类已覆盖；再处理模板字符串 `...`
    html = re.sub(r"`(" + re.escape(prefix) + r")?/api/", "`" + prefix + "/api/", html)
    return html


def rewrite_body_bytes(body: bytes, content_type: str) -> bytes:
    """按内容类型决定是否重写响应体。"""
    if "text/html" in content_type:
        try:
            return rewrite_html(body.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return body
    return body


def filter_headers(headers: dict[str, str], *, drop_length: bool) -> dict[str, str]:
    """过滤 hop-by-hop 头；重写响应体时需要去掉 content-length。"""
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _HOP_HEADERS:
            continue
        if drop_length and lk == "content-length":
            continue
        out[k] = v
    return out


async def proxy_request(
    manager: BTGuardManager,
    method: str,
    path: str,
    *,
    query: str = "",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """把一个请求转发到 BT 子服务。

    :param path: 已去掉 ``/btg`` 前缀的路径，例如 ``/api/status``
    :return: (status_code, headers, body)
    """
    client = manager._client()  # noqa: SLF001 - 同模块协作
    url = path
    if query:
        url = f"{path}?{query}"

    fwd_headers = filter_headers(headers or {}, drop_length=False)
    # 告诉 Go 侧真实的前缀，便于它生成正确的链接
    fwd_headers["X-Forwarded-Prefix"] = PROXY_PREFIX

    import httpx

    try:
        resp = await client.request(
            method, url, content=body, headers=fwd_headers
        )
    except httpx.ConnectError as exc:
        raise _proxy_unavailable(str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise _proxy_unavailable(f"请求超时: {exc}") from exc
    except httpx.HTTPError as exc:
        raise _proxy_unavailable(str(exc)) from exc

    content_type = resp.headers.get("content-type", "")
    out_body = rewrite_body_bytes(resp.content, content_type)
    rewritten = out_body is not resp.content

    out_headers = filter_headers(dict(resp.headers), drop_length=rewritten)
    return resp.status_code, out_headers, out_body


class _proxy_unavailable(Exception):
    """子服务不可用（供路由层转成 503）。"""


# ===========================================================================
# 全局单例
# ===========================================================================
_manager: BTGuardManager | None = None


def get_btg_manager() -> BTGuardManager:
    """获取进程内唯一的 BT 子服务管理器。"""
    global _manager
    if _manager is None:
        _manager = BTGuardManager()
    return _manager
