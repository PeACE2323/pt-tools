"""PT 流量监控服务：采集编排 + 配置持久化。

替代原 Go 子服务（bt-traffic-guard）。整合后的形态：

    PTtools（单进程）
      ├── bt_client.py   下载器客户端（qB / Transmission）
      ├── bt_policy.py   限速策略引擎
      └── bt_monitor.py  本文件：采集循环、配置读写、快照缓存

配置存放在 PTtools 自己的 SQLite 里（``app_settings`` 表，key 为
``pt.clients`` / ``pt.policies``），不再依赖 YAML 文件。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from .bt_client import BaseClient, ClientError, ClientSnapshot, make_client
from .bt_policy import PolicyConfig, PolicyEngine
from ..utils import get_logger, utcnow_ts

logger = get_logger("guard.bt.monitor")


#: 配置在 app_settings 中的 key
SETTING_CLIENTS = "pt.clients"
SETTING_POLICIES = "pt.policies"
#: 策略运行时状态（周期基准、限速标记），与用户配置分开存
SETTING_RUNTIME = "pt.runtime"


# ===========================================================================
# 配置读写
# ===========================================================================
def load_pt_config(session) -> tuple[list[dict], list[dict]]:
    """从数据库读取 PT 配置，返回 (clients, policies)。

    首次运行返回空列表 —— 由用户在前端添加。
    """
    from ..models import AppSetting

    def _get(key: str) -> list[dict]:
        row = session.get(AppSetting, key)
        if row is None or not row.value:
            return []
        try:
            data = json.loads(row.value)
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            logger.warning("配置 %s 解析失败，已忽略", key)
            return []

    return _get(SETTING_CLIENTS), _get(SETTING_POLICIES)


def save_pt_config(
    session, clients: list[dict] | None = None, policies: list[dict] | None = None
) -> None:
    """写入 PT 配置。只写传入的部分。"""
    from ..models import AppSetting
    from datetime import datetime, timezone

    def _set(key: str, value: list[dict]) -> None:
        text = json.dumps(value, ensure_ascii=False)
        row = session.get(AppSetting, key)
        if row is None:
            session.add(
                AppSetting(
                    key=key,
                    value=text,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        else:
            row.value = text
            row.updated_at = datetime.now(timezone.utc)

    if clients is not None:
        _set(SETTING_CLIENTS, clients)
    if policies is not None:
        _set(SETTING_POLICIES, policies)


# ---------------------------------------------------------------- 运行时状态
def load_pt_runtime(session) -> dict:
    """读取策略运行时状态（周期基准、限速标记）。

    与配置分开存：配置是用户编辑的，运行时状态是引擎自己维护的。
    """
    from ..models import AppSetting

    row = session.get(AppSetting, SETTING_RUNTIME)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        logger.warning("运行时状态解析失败，已忽略")
        return {}


def save_pt_runtime(session, state: dict) -> None:
    """写入策略运行时状态。"""
    from datetime import datetime, timezone

    from ..models import AppSetting

    text = json.dumps(state, ensure_ascii=False)
    row = session.get(AppSetting, SETTING_RUNTIME)
    if row is None:
        session.add(
            AppSetting(
                key=SETTING_RUNTIME,
                value=text,
                updated_at=datetime.now(timezone.utc),
            )
        )
    else:
        row.value = text
        row.updated_at = datetime.now(timezone.utc)


def persist_pt_runtime(engine) -> None:
    """把引擎状态落盘（供采集循环定期调用）。"""
    from ..models import session_scope

    try:
        state = engine.export_state()
        with session_scope() as session:
            save_pt_runtime(session, state)
    except Exception as exc:  # noqa: BLE001 - 落盘失败不影响采集
        logger.debug("保存 PT 运行时状态失败: %s", exc)


# ===========================================================================
# 监控器
# ===========================================================================
@dataclass
class MonitorSnapshot:
    """一次完整采集的结果。"""

    ts: float = field(default_factory=utcnow_ts)
    clients: list[ClientSnapshot] = field(default_factory=list)

    def aggregate(self) -> dict[str, int]:
        return {
            "up_speed": sum(c.up_speed for c in self.clients),
            "dl_speed": sum(c.dl_speed for c in self.clients),
            "up_total": sum(c.up_total for c in self.clients),
            "torrents": sum(c.torrents for c in self.clients),
            "peers": sum(c.peers for c in self.clients),
        }


class PTMonitor:
    """PT 流量监控器：定期采集 → 求值策略 → 缓存快照。"""

    def __init__(self) -> None:
        self.engine = PolicyEngine()
        self._clients: dict[str, BaseClient] = {}
        self._configs: list[dict] = []
        self._policy_configs: list[dict] = []
        self._snapshot = MonitorSnapshot()
        self._lock = asyncio.Lock()
        self._last_error = ""
        self._loaded = False
        # 采集轮次计数，用于控制运行时状态的落盘频率
        self._poll_count = 0

    # ------------------------------------------------------------ 配置
    async def reload(self) -> None:
        """从数据库重载配置并重建客户端。"""
        from ..models import session_scope

        with session_scope() as session:
            clients, policies = load_pt_config(session)
            runtime = load_pt_runtime(session)

        self._configs = clients
        self._policy_configs = policies

        # 关掉旧客户端
        await self._close_clients()

        new_clients: dict[str, BaseClient] = {}
        for cfg in clients:
            name = str(cfg.get("name") or "").strip()
            if not name or cfg.get("enabled") is False:
                continue
            try:
                new_clients[name] = make_client(cfg)
            except Exception as exc:  # noqa: BLE001
                logger.error("创建客户端 %s 失败: %s", name, exc)
        self._clients = new_clients

        self.engine.apply_config(
            [PolicyConfig.from_dict(p) for p in policies if p.get("name")]
        )
        # 恢复上次的周期基准与限速状态。
        # 否则容器重启后本周期流量会从 0 重新计算（用户看到"清零"）。
        # 若重启跨越了周期边界，evaluate() 会检测到 window_key 变化并
        # 正常重置——那是新周期应有的行为。
        if runtime:
            self.engine.import_state(runtime)
            restored = sum(
                1 for v in runtime.values() if v.get("window_key")
            )
            if restored:
                logger.info("已恢复 %d 条策略的周期进度", restored)

        self._loaded = True
        logger.info(
            "PT 配置已加载：%d 个下载器，%d 条策略",
            len(new_clients), len(self.engine.configs()),
        )

    async def _close_clients(self) -> None:
        for c in self._clients.values():
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        await self._close_clients()
        self._clients = {}

    @property
    def client_names(self) -> list[str]:
        return list(self._clients.keys())

    # ------------------------------------------------------------ 采集
    async def poll_once(self) -> MonitorSnapshot:
        """采集一轮并求值策略。"""
        async with self._lock:
            if not self._loaded:
                await self.reload()

            if not self._clients:
                self._snapshot = MonitorSnapshot(ts=utcnow_ts())
                return self._snapshot

            # 并发采集，单个失败不影响其它
            names = list(self._clients)
            results = await asyncio.gather(
                *(self._clients[n].fetch() for n in names),
                return_exceptions=True,
            )

            snaps: list[ClientSnapshot] = []
            errors: list[str] = []
            for name, res in zip(names, results):
                if isinstance(res, ClientSnapshot):
                    snaps.append(res)
                    if not res.healthy and res.last_error:
                        errors.append(f"{name}: {res.last_error}")
                else:
                    # fetch 内部已兜住异常，走到这里说明是意外
                    client = self._clients[name]
                    snap = ClientSnapshot(
                        name=name,
                        type=client.type_name,
                        url=client.url,
                        healthy=False,
                        last_error=str(res),
                    )
                    snaps.append(snap)
                    errors.append(f"{name}: {res}")

            self._snapshot = MonitorSnapshot(ts=utcnow_ts(), clients=snaps)
            self._last_error = "; ".join(errors)

            # 求值策略
            try:
                await self.engine.evaluate(
                    {s.name: s.to_dict() for s in snaps},
                    self._clients,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("策略求值失败")
                self._last_error = f"策略求值失败: {exc}"

            # 定期把周期基准落盘，避免重启后本周期流量归零。
            # 用计数控制频率，不必每轮都写。
            self._poll_count += 1
            if self._poll_count % 4 == 0:
                persist_pt_runtime(self.engine)

            return self._snapshot

    # ------------------------------------------------------------ 查询
    def snapshot(self) -> MonitorSnapshot:
        return self._snapshot

    def overview(self) -> dict[str, Any]:
        """给前端的聚合视图。"""
        snap = self._snapshot
        agg = snap.aggregate()
        clients = [c.to_dict() for c in snap.clients]
        states = [s.to_dict() for s in self.engine.states()]

        return {
            "running": True,
            "configured": bool(self._configs),
            "client_count": len(clients),
            "client_online": sum(1 for c in clients if c["healthy"]),
            "policy_count": len(states),
            "policy_limited": sum(1 for s in states if s["limited"]),
            "up_speed": agg["up_speed"],
            "dl_speed": agg["dl_speed"],
            "up_total": agg["up_total"],
            "window_bytes": sum(s["current_total"] for s in states),
            "torrents": agg["torrents"],
            "peers": agg["peers"],
            "last_error": self._last_error,
            "updated_at": snap.ts,
        }

    def clients_view(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._snapshot.clients]

    def policies_view(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.engine.states()]

    # ------------------------------------------------------------ 手动操作
    async def test_client(self, name: str) -> dict[str, Any]:
        """测试单个下载器连通性。"""
        client = self._clients.get(name)
        if client is None:
            # 可能配置了但未启用，或刚保存还没重载
            for cfg in self._configs:
                if cfg.get("name") == name:
                    client = make_client(cfg)
                    break
        if client is None:
            raise ClientError(f"未找到客户端 {name}")

        snap = await client.fetch()
        result = {
            "ok": snap.healthy,
            "name": snap.name,
            "type": snap.type,
            "version": snap.version,
            "up_speed": snap.up_speed,
            "up_total": snap.up_total,
            "dl_speed": snap.dl_speed,
            "torrents": snap.torrents,
            "peers": snap.peers,
            "current_up_limit": snap.current_up_limit,
        }
        if not snap.healthy:
            result["error"] = snap.last_error
            result["message"] = f"{name} 连接失败：{snap.last_error}"
        else:
            result["message"] = f"{name} 连接正常"
        return result

    async def policy_action(self, action: str, name: str) -> dict[str, Any]:
        """策略操作：resume / trigger。"""
        if action == "resume":
            changed = await self.engine.force_resume(name, self._clients)
            msg = (
                f"已解除策略「{name}」的限速"
                if changed
                else f"策略「{name}」当前未限速"
            )
        elif action == "trigger":
            changed = await self.engine.force_trigger(name, self._clients)
            msg = (
                f"已手动触发策略「{name}」"
                if changed
                else f"策略「{name}」已在限速状态"
            )
        else:
            raise ValueError(f"不支持的操作: {action}")
        return {"ok": True, "changed": changed, "message": msg}

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.engine.events(limit)


# ===========================================================================
# 全局单例
# ===========================================================================
_monitor: PTMonitor | None = None


def get_pt_monitor() -> PTMonitor:
    """获取进程内唯一的 PT 监控器。"""
    global _monitor
    if _monitor is None:
        _monitor = PTMonitor()
    return _monitor
