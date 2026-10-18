"""BT 下载器客户端（qBittorrent / Transmission）。

从 Go 版 bt-traffic-guard 移植而来，协议细节保持一致，详见各方法注释。

两类客户端只做三件事：

1. **采集**：上传速度、累计上传量、种子数、连接数、当前限速
2. **限速**：下发 / 解除上传限速
3. **探活**：测试连通性并给出可操作错误

并发模型：全部使用 ``httpx.AsyncClient``，调用方负责控制频率。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..utils import get_logger

logger = get_logger("guard.bt.client")


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass(slots=True)
class ClientSnapshot:
    """一次采集的结果。字段名与 Go 版 /api/status 的 JSON 保持一致。"""

    name: str
    type: str
    url: str
    healthy: bool = False
    last_error: str = ""
    version: str = ""
    # 速率与流量（字节 / 字节每秒）
    up_speed: int = 0
    up_total: int = 0
    dl_speed: int = 0
    # 规模
    torrents: int = 0
    peers: int = 0
    # 当前生效的上传限速（0 = 不限）
    current_up_limit: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "healthy": self.healthy,
            "last_error": self.last_error,
            "version": self.version,
            "up_speed": self.up_speed,
            "up_total": self.up_total,
            "dl_speed": self.dl_speed,
            "torrents": self.torrents,
            "peers": self.peers,
            "current_up_limit": self.current_up_limit,
        }


class ClientError(Exception):
    """下载器交互失败，message 面向用户。"""


# ===========================================================================
# 基类
# ===========================================================================
class BaseClient:
    """客户端基类。"""

    type_name = "base"

    def __init__(
        self,
        name: str,
        url: str,
        *,
        username: str = "",
        password: str = "",
        api_key: str = "",
        timeout: float = 10.0,
        verify_tls: bool = True,
    ) -> None:
        self.name = name
        self.url = (url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.api_key = api_key or ""
        self.timeout = timeout
        self.verify_tls = verify_tls

    async def fetch(self) -> ClientSnapshot:
        raise NotImplementedError

    async def set_upload_limit(self, limit: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


# ===========================================================================
# qBittorrent
# ===========================================================================
class QBittorrentClient(BaseClient):
    """qBittorrent WebUI API 客户端。

    认证有两条路：

    * **API Key**（v5.2.0+）：``Authorization: Bearer qbt_xxx``，无状态，永不触发 IP 封禁 —— 优先使用
    * **用户名密码**：``POST /api/v2/auth/login`` 拿 SID Cookie，需定期重新登录

    关键接口：

        POST /api/v2/auth/login                 登录
        GET  /api/v2/transfer/info              全局传输信息
        GET  /api/v2/sync/maindata              含 alltime_ul（累计上传）
        GET  /api/v2/torrents/info              种子列表
        GET  /api/v2/app/version                版本
        POST /api/v2/transfer/setUploadLimit    设置全局上传限速（0=不限）
        GET  /api/v2/app/preferences            读取限速设置
    """

    type_name = "qbittorrent"

    #: 登录有效期，超过后重新登录（避免长期失效）
    _AUTH_TTL = 20 * 60

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http: Any = None
        self._logged_in = False
        self._last_auth = 0.0
        self._auth_lock = asyncio.Lock()

    def _client(self):
        import httpx

        if self._http is None or getattr(self._http, "is_closed", True):
            self._http = httpx.AsyncClient(
                base_url=self.url,
                timeout=httpx.Timeout(self.timeout, connect=min(5.0, self.timeout)),
                verify=self.verify_tls,
                follow_redirects=True,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not getattr(self._http, "is_closed", True):
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._http = None

    # ------------------------------------------------------------ 认证
    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    async def _ensure_login(self) -> None:
        """确保已登录（仅在使用用户名密码时有意义）。"""
        if self.api_key:
            return

        import time

        async with self._auth_lock:
            if self._logged_in and time.time() - self._last_auth < self._AUTH_TTL:
                return

            if not self.username and not self.password:
                raise ClientError(
                    f"客户端 {self.name} 未配置认证信息：请填写 "
                    "API Key（qBittorrent 5.2.0+）或用户名密码"
                )

            client = self._client()
            form = {
                "username": self.username,
                "password": self.password,
                "referer": self.url,
            }
            try:
                resp = await client.post("/api/v2/auth/login", data=form)
            except Exception as exc:  # noqa: BLE001
                raise ClientError(f"登录 qBittorrent 失败: {exc}") from exc

            if resp.status_code == 403:
                raise ClientError(
                    "登录 qBittorrent 被拒绝（HTTP 403）：连续失败次数过多，"
                    "请在 WebUI 中解除 IP 封禁，或检查账号密码"
                )
            if resp.status_code == 401:
                raise ClientError(
                    "登录 qBittorrent 失败（HTTP 401）：用户名或密码不正确。"
                    "若版本 ≥ 5.2.0，建议改用 API Key 认证以避免 IP 封禁"
                )
            if resp.status_code != 200:
                raise ClientError(
                    f"登录 qBittorrent 失败: HTTP {resp.status_code} "
                    f"{resp.text[:120]}"
                )

            body = (resp.text or "").strip().lower()
            if body not in ("ok.", "ok"):
                if "banned" in body:
                    raise ClientError("登录 qBittorrent 失败：IP 已被临时封禁")
                if "fail" in body:
                    raise ClientError("登录 qBittorrent 失败：用户名或密码错误")
                raise ClientError(f"登录 qBittorrent 返回未知结果: {body[:80]}")

            self._logged_in = True
            self._last_auth = time.time()

    def _invalidate_auth(self) -> None:
        self._logged_in = False

    # ------------------------------------------------------------ 请求
    async def _get(self, path: str, *, _retry: bool = True) -> Any:
        await self._ensure_login()
        client = self._client()
        headers = self._auth_headers()
        try:
            resp = await client.get(path, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise ClientError(f"请求 {path} 失败: {exc}") from exc

        if resp.status_code == 403 and _retry and not self.api_key:
            # Cookie 失效：重新登录后重试一次
            self._invalidate_auth()
            await self._ensure_login()
            return await self._get(path, _retry=False)
        if resp.status_code >= 400:
            raise ClientError(
                f"请求 {path} 返回 HTTP {resp.status_code}: {resp.text[:120]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ClientError(f"{path} 返回的不是 JSON") from exc

    async def _post_form(self, path: str, form: dict[str, Any], *, _retry: bool = True) -> None:
        await self._ensure_login()
        client = self._client()
        headers = self._auth_headers()
        try:
            resp = await client.post(path, data=form, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise ClientError(f"请求 {path} 失败: {exc}") from exc

        if resp.status_code == 403 and _retry and not self.api_key:
            self._invalidate_auth()
            await self._ensure_login()
            return await self._post_form(path, form, _retry=False)
        if resp.status_code >= 400:
            raise ClientError(
                f"请求 {path} 返回 HTTP {resp.status_code}: {resp.text[:120]}"
            )

    # ------------------------------------------------------------ 采集
    async def fetch(self) -> ClientSnapshot:
        snap = ClientSnapshot(
            name=self.name, type=self.type_name, url=self.url
        )
        try:
            # 1) 全局传输信息
            info = await self._get("/api/v2/transfer/info") or {}
            snap.up_speed = int(info.get("up_info_speed") or 0)
            snap.up_total = int(info.get("up_info_data") or 0)
            snap.dl_speed = int(info.get("dl_info_speed") or 0)

            # 2) 累计上传（alltime_ul 比 up_info_data 更接近「历史总量」）
            try:
                main = await self._get("/api/v2/sync/maindata") or {}
                server = main.get("server_state") or {}
                alltime = int(server.get("alltime_ul") or 0)
                if alltime > 0:
                    snap.up_total = alltime
            except ClientError:
                pass

            # 3) 种子与连接数
            try:
                torrents = await self._get("/api/v2/torrents/info") or []
                snap.torrents = len(torrents) if isinstance(torrents, list) else 0
                peers = 0
                for t in torrents if isinstance(torrents, list) else []:
                    peers += int(t.get("num_leechs") or 0)
                    peers += int(t.get("num_seeds") or 0)
                snap.peers = peers
            except ClientError:
                pass

            # 4) 版本
            try:
                ver = await self._get("/api/v2/app/version")
                snap.version = str(ver).strip().strip('"')
            except ClientError:
                pass

            # 5) 当前上传限速
            try:
                prefs = await self._get("/api/v2/app/preferences") or {}
                if prefs.get("up_limit") is not None:
                    snap.current_up_limit = int(prefs.get("up_limit") or 0)
            except ClientError:
                pass

            snap.healthy = True
        except ClientError as exc:
            snap.healthy = False
            snap.last_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            snap.healthy = False
            snap.last_error = f"{exc.__class__.__name__}: {exc}"
        return snap

    # ------------------------------------------------------------ 限速
    async def set_upload_limit(self, limit: int) -> None:
        """下发全局上传限速（字节/秒，0 = 不限速）。"""
        if limit < 0:
            return
        await self._post_form(
            "/api/v2/transfer/setUploadLimit", {"limit": str(int(limit))}
        )

    async def get_upload_limit(self) -> int:
        prefs = await self._get("/api/v2/app/preferences") or {}
        return int(prefs.get("up_limit") or 0)


# ===========================================================================
# Transmission
# ===========================================================================
class TransmissionClient(BaseClient):
    """Transmission RPC 客户端。

    协议要点：首次请求返回 **409**，响应头 ``X-Transmission-Session-Id``
    给出会话号；后续请求必须回填该头，否则永远 409。

    使用的方法：

        session-get          读取 speed-limit-up / speed-limit-up-enabled
        session-stats        全局速率与累计量
        torrent-get          种子与 peer 详情
        session-set          设置上传限速
    """

    type_name = "transmission"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http: Any = None
        self._session_id = ""
        self._lock = asyncio.Lock()

    def _client(self):
        import httpx

        if self._http is None or getattr(self._http, "is_closed", True):
            auth = None
            if self.username or self.password:
                auth = httpx.BasicAuth(self.username, self.password)
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(5.0, self.timeout)),
                verify=self.verify_tls,
                auth=auth,
                follow_redirects=True,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not getattr(self._http, "is_closed", True):
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._http = None

    @property
    def rpc_url(self) -> str:
        """推导 RPC 端点。

        用户可能填 ``http://host:9091`` 或 ``http://host:9091/transmission/rpc``，
        两种都要能正确工作。
        """
        base = self.url
        if not base:
            raise ClientError("Transmission 地址为空")
        if base.endswith("/transmission/rpc"):
            return base
        return base + "/transmission/rpc"

    async def _call(self, method: str, args: dict | None = None) -> dict:
        """执行一次 RPC，自动处理 409 会话握手。"""
        import httpx

        client = self._client()
        payload = {"method": method, "arguments": args or {}}

        for attempt in range(2):
            headers = {"Content-Type": "application/json"}
            if self._session_id:
                headers["X-Transmission-Session-Id"] = self._session_id
            try:
                resp = await client.post(self.rpc_url, json=payload, headers=headers)
            except Exception as exc:  # noqa: BLE001
                raise ClientError(f"请求 Transmission 失败: {exc}") from exc

            if resp.status_code == 409:
                sid = resp.headers.get("X-Transmission-Session-Id", "")
                if not sid:
                    raise ClientError(
                        "Transmission 返回 409 但未提供 Session-Id，"
                        "请确认地址指向 /transmission/rpc"
                    )
                self._session_id = sid
                continue

            if resp.status_code == 401:
                raise ClientError("Transmission 认证失败（HTTP 401）：请检查用户名密码")
            if resp.status_code >= 400:
                raise ClientError(
                    f"Transmission 返回 HTTP {resp.status_code}: {resp.text[:120]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ClientError("Transmission 返回的不是 JSON") from exc

            if data.get("result") not in ("success", None):
                raise ClientError(f"Transmission 返回错误: {data.get('result')}")
            return data.get("arguments") or {}

        raise ClientError("Transmission 会话握手失败（多次 409）")

    # ------------------------------------------------------------ 采集
    async def fetch(self) -> ClientSnapshot:
        snap = ClientSnapshot(name=self.name, type=self.type_name, url=self.url)
        try:
            stats = await self._call("session-stats")
            snap.up_speed = int(stats.get("uploadSpeed") or 0)
            snap.up_total = int(stats.get("cumulative-stats", {}).get("uploadedBytes") or 0)
            snap.dl_speed = int(stats.get("downloadSpeed") or 0)
            snap.torrents = int(stats.get("activeTorrentCount") or 0)

            sess = await self._call("session-get")
            snap.version = str(sess.get("version") or "")
            # Transmission 的限速单位是 KB/s，需要换算成字节/秒
            limit_kb = int(sess.get("speed-limit-up") or 0)
            enabled = bool(sess.get("speed-limit-up-enabled"))
            snap.current_up_limit = limit_kb * 1024 if enabled else 0

            # 连接数：从种子列表累加
            try:
                res = await self._call(
                    "torrent-get",
                    {"fields": ["peers", "leechers", "seeders"]},
                )
                total_peers = 0
                torrents = res.get("torrents") or []
                for t in torrents:
                    peers = t.get("peers")
                    if isinstance(peers, list):
                        total_peers += len(peers)
                    else:
                        total_peers += int(t.get("leechers") or 0)
                        total_peers += int(t.get("seeders") or 0)
                snap.peers = total_peers
                if not snap.torrents:
                    snap.torrents = len(torrents)
            except ClientError:
                pass

            snap.healthy = True
        except ClientError as exc:
            snap.healthy = False
            snap.last_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            snap.healthy = False
            snap.last_error = f"{exc.__class__.__name__}: {exc}"
        return snap

    # ------------------------------------------------------------ 限速
    async def set_upload_limit(self, limit: int) -> None:
        """下发上传限速（入参字节/秒，Transmission 用 KB/s）。"""
        if limit < 0:
            return
        if limit == 0:
            await self._call(
                "session-set", {"speed-limit-up-enabled": False}
            )
            return
        await self._call(
            "session-set",
            {
                "speed-limit-up": max(1, int(limit) // 1024),
                "speed-limit-up-enabled": True,
            },
        )


# ===========================================================================
# 工厂
# ===========================================================================
def make_client(cfg: dict[str, Any]) -> BaseClient:
    """按配置创建客户端。

    :param cfg: 来自表单的下载器配置字典
    """
    ctype = (cfg.get("type") or "qbittorrent").strip().lower()
    name = str(cfg.get("name") or "").strip()
    url = str(cfg.get("url") or "").strip()

    timeout_raw = cfg.get("timeout") or "10s"
    timeout = _parse_timeout(timeout_raw)

    common = dict(
        name=name,
        url=url,
        username=str(cfg.get("username") or ""),
        password=str(cfg.get("password") or ""),
        timeout=timeout,
        verify_tls=not bool(cfg.get("insecure_skip_verify", False)),
    )

    if ctype == "transmission":
        return TransmissionClient(**common)
    return QBittorrentClient(api_key=str(cfg.get("api_key") or ""), **common)


def _parse_timeout(value: Any) -> float:
    """把 ``"10s"`` / ``"500ms"`` / ``10`` 解析成秒。"""
    if isinstance(value, (int, float)):
        return float(value) or 10.0
    raw = str(value or "").strip().lower()
    if not raw:
        return 10.0
    try:
        if raw.endswith("ms"):
            return max(0.5, float(raw[:-2]) / 1000.0)
        if raw.endswith("s"):
            return max(0.5, float(raw[:-1]))
        if raw.endswith("m"):
            return max(0.5, float(raw[:-1]) * 60)
        return max(0.5, float(raw))
    except ValueError:
        return 10.0
