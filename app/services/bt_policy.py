"""限速策略引擎。

从 Go 版 bt-traffic-guard 的 policy/engine.go 移植。

核心语义：**在一个统计周期内，上传总量达到阈值就自动限速，进入新周期自动解除。**

## 周期与对齐

``window`` 是周期长度（如 24h / 7d），``align`` 决定边界如何计算：

* ``day``    —— 自然日：每天 00:00 重置
* ``hour``   —— 自然小时
* ``week``   —— 自然周（周一 00:00 起）
* ``month``  —— 自然月
* ``rolling``—— 滚动窗口：从首次观测（或进程启动）开始滚动

自然周期用「本地时区」计算边界，滚动周期用固定长度。

## 触发与解除

    采集到 累计上传 ↑
        ↓
    周期内增量 = 当前累计 - 周期起点累计
        ↓
    增量 ≥ threshold 且 未限速  →  下发 limit_total，标记 limited
        ↓
    进入新周期 / 手动解除  →  恢复原始限速（或 resume_limit_total）

## 为什么用「累计量差值」而不是「速率积分」

速率采样有丢包、抖动、重启归零等问题，积分误差会累积。
用客户端自己维护的累计上传字节数做差，是最稳的做法。
但累计量在客户端重启后会归零（变小），所以检测到回退时要把基线拉回。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..utils import get_logger, utcnow_ts

logger = get_logger("guard.bt.policy")


# ===========================================================================
# 周期解析
# ===========================================================================
def parse_duration(value: Any, default_sec: int = 86400) -> int:
    """解析 ``24h`` / ``7d`` / ``30m`` / ``3600`` 为秒。"""
    if isinstance(value, (int, float)):
        return max(1, int(value))
    raw = str(value or "").strip().lower()
    if not raw:
        return default_sec
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        if raw[-1] in units:
            return max(1, int(float(raw[:-1]) * units[raw[-1]]))
        return max(1, int(float(raw)))
    except (ValueError, IndexError):
        return default_sec


def window_bounds(
    align: str, window_sec: int, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """计算当前统计周期的 [起点, 终点)。

    :param align: day / hour / week / month / rolling
    :param window_sec: 周期长度（rolling 模式用它；自然周期以实际边界为准）
    """
    now = now or datetime.now()

    align = (align or "day").lower()

    if align == "hour":
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)

    if align == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    if align == "week":
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Monday=0
        start = day_start - timedelta(days=now.weekday())
        return start, start + timedelta(days=7)

    if align == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days = calendar.monthrange(start.year, start.month)[1]
        return start, start + timedelta(days=days)

    # rolling：以 window_sec 为长度，锚定在「现在之前最近的窗口倍数」上
    # 这里用固定长度滚动：起点 = now - window_sec，但为了让进度可读，
    # 采用「整周期对齐」：把 now 对齐到 window_sec 的整数倍
    epoch = int(now.timestamp())
    start_ts = (epoch // window_sec) * window_sec
    start = datetime.fromtimestamp(start_ts)
    return start, start + timedelta(seconds=window_sec)


def format_bytes(n: float) -> str:
    """人类可读的字节数。"""
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class PolicyConfig:
    """一条限速策略的配置。"""

    name: str
    enabled: bool = True
    clients: list[str] = field(default_factory=list)  # 空 = 全部
    window: str = "24h"
    align: str = "day"
    threshold_total: int = 0  # 字节
    limit_total: int = 0  # 字节/秒，0 = 不限
    resume_limit_total: int = 0  # >0 时用它作为恢复速度
    resume_on_window_reset: bool = True
    cooldown: str = "0s"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PolicyConfig:
        return cls(
            name=str(d.get("name") or ""),
            enabled=bool(d.get("enabled", True)),
            clients=list(d.get("clients") or []),
            window=str(d.get("window") or "24h"),
            align=str(d.get("align") or "day"),
            threshold_total=_parse_bytes(d.get("threshold_total")),
            limit_total=_parse_bitrate(d.get("limit_total")),
            resume_limit_total=_parse_bitrate(d.get("resume_limit_total")),
            resume_on_window_reset=bool(d.get("resume_on_window_reset", True)),
            cooldown=str(d.get("cooldown") or "0s"),
        )

    def applies_to(self, client_name: str) -> bool:
        if not self.clients:
            return True
        for n in self.clients:
            if n == "*" or n.lower() == client_name.lower():
                return True
        return False


def _parse_bytes(value: Any) -> int:
    """解析 ``500GiB`` / ``500G`` / ``536870912000``。"""
    if isinstance(value, (int, float)):
        return max(0, int(value))
    raw = str(value or "").strip().lower().replace(" ", "")
    if not raw:
        return 0
    units = {
        "b": 1,
        "k": 1024, "kb": 1024, "kib": 1024,
        "m": 1024**2, "mb": 1024**2, "mib": 1024**2,
        "g": 1024**3, "gb": 1024**3, "gib": 1024**3,
        "t": 1024**4, "tb": 1024**4, "tib": 1024**4,
        "p": 1024**5, "pb": 1024**5, "pib": 1024**5,
    }
    # 从长到短匹配，避免 "gib" 被 "b" 抢先
    for suffix in sorted(units, key=len, reverse=True):
        if raw.endswith(suffix):
            num = raw[: -len(suffix)]
            try:
                return max(0, int(float(num) * units[suffix]))
            except ValueError:
                return 0
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


def _parse_bitrate(value: Any) -> int:
    """限速值同样支持 1M / 512K / 1048576（字节/秒）。"""
    return _parse_bytes(value)


# ===========================================================================
# 运行时状态
# ===========================================================================
@dataclass
class PolicyState:
    """一条策略的运行时状态（用于展示与判断）。"""

    name: str
    enabled: bool = True
    align: str = ""
    window: str = ""
    window_start: float = 0.0
    window_end: float = 0.0
    window_label: str = ""
    threshold_total: int = 0
    limit_total: int = 0
    clients: list[str] = field(default_factory=list)
    limited: bool = False
    limited_at: float = 0.0
    limited_for: str = ""
    current_total: int = 0
    progress_total: float = 0.0
    last_error: str = ""
    cooldown: str = ""
    resume_on_window_reset: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "align": self.align,
            "window": self.window,
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "window_label": self.window_label,
            "threshold_total": self.threshold_total,
            "limit_total": self.limit_total,
            "clients": self.clients,
            "limited": self.limited,
            "limited_at": _iso(self.limited_at),
            "limited_for": self.limited_for,
            "current_total": self.current_total,
            "progress_total": self.progress_total,
            "last_error": self.last_error,
            "cooldown": self.cooldown,
            "resume_on_window_reset": self.resume_on_window_reset,
        }


def _iso(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


@dataclass
class PolicyRuntime:
    """策略的内部可变状态。"""

    config: PolicyConfig
    # 周期起点时各客户端的累计上传量，用于算增量
    baseline: dict[str, int] = field(default_factory=dict)
    window_key: str = ""
    limited: bool = False
    limited_at: float = 0.0
    # 限速前的原始限速，用于恢复
    original_limits: dict[str, int] = field(default_factory=dict)
    # 最近一次算出的「本周期已用」
    #
    # 注意：必须与 baseline 分开存。早期实现把已用量塞进 baseline
    # （用 "__used__" 作键），结果 states() 里 sum(baseline.values())
    # 会把已用量重复累加，进度条显示翻倍。
    used: int = 0


# ===========================================================================
# 引擎
# ===========================================================================
class PolicyEngine:
    """限速策略引擎。

    与 Go 版一致：策略之间**独立求值**，互不影响。
    多条策略同时命中时，取最严格的限速（最小值，0 视为不限即最宽松）。
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, PolicyRuntime] = {}
        self._events: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 配置
    def apply_config(self, policies: list[PolicyConfig]) -> None:
        """应用新配置。

        保留已有运行时状态（baseline / limited），这样热更新配置
        不会把当前周期的进度与限速状态清空。
        """
        wanted = {p.name: p for p in policies if p.name}
        # 删除已不存在的策略
        for name in list(self._runtimes):
            if name not in wanted:
                self._runtimes.pop(name, None)
        # 新增或更新
        for name, cfg in wanted.items():
            rt = self._runtimes.get(name)
            if rt is None:
                self._runtimes[name] = PolicyRuntime(config=cfg)
            else:
                old = rt.config
                rt.config = cfg
                # 窗口或阈值变了，重置基线避免用旧口径算增量
                if (
                    old.window != cfg.window
                    or old.align != cfg.align
                    or old.threshold_total != cfg.threshold_total
                ):
                    rt.baseline.clear()
                    rt.window_key = ""

    def configs(self) -> list[PolicyConfig]:
        return [rt.config for rt in self._runtimes.values()]

    # ------------------------------------------------------------ 状态持久化
    def export_state(self) -> dict[str, Any]:
        """导出运行时状态，供重启后恢复。

        **为什么需要**：``baseline`` 记录周期起点时各客户端的累计上传量，
        ``window_key`` 记录当前周期。它们原先只在内存里，容器重启后
        全部丢失 —— 第一轮采集会把「当前累计量」当成新基线，
        导致本周期流量从 0 重新开始计算。

        导出内容不含敏感信息，只有计量基准与限速状态。
        """
        out: dict[str, Any] = {}
        for name, rt in self._runtimes.items():
            out[name] = {
                "window_key": rt.window_key,
                "baseline": dict(rt.baseline),
                "limited": rt.limited,
                "limited_at": rt.limited_at,
                "original_limits": dict(rt.original_limits),
                "used": rt.used,
            }
        return out

    def import_state(self, state: dict[str, Any] | None) -> None:
        """恢复运行时状态。

        只恢复「周期仍有效」的部分：若重启跨越了周期边界，
        ``evaluate()`` 会自动检测到 ``window_key`` 不匹配并重置，
        这是正确行为（新周期就该从 0 算）。
        """
        if not isinstance(state, dict):
            return
        for name, data in state.items():
            rt = self._runtimes.get(name)
            if rt is None or not isinstance(data, dict):
                continue
            baseline = data.get("baseline")
            if isinstance(baseline, dict):
                rt.baseline = {
                    str(k): int(v)
                    for k, v in baseline.items()
                    if isinstance(v, (int, float))
                }
            rt.window_key = str(data.get("window_key") or "")
            rt.limited = bool(data.get("limited"))
            rt.limited_at = float(data.get("limited_at") or 0)
            limits = data.get("original_limits")
            if isinstance(limits, dict):
                rt.original_limits = {
                    str(k): int(v)
                    for k, v in limits.items()
                    if isinstance(v, (int, float))
                }
            rt.used = int(data.get("used") or 0)

    # ------------------------------------------------------------ 状态
    def states(self) -> list[PolicyState]:
        now = datetime.now()
        out: list[PolicyState] = []
        for rt in self._runtimes.values():
            cfg = rt.config
            start, end = window_bounds(cfg.align, parse_duration(cfg.window), now)
            total = rt.used
            progress = (
                (total / cfg.threshold_total * 100.0)
                if cfg.threshold_total > 0
                else 0.0
            )
            limited_for = ""
            if rt.limited and rt.limited_at:
                secs = int(utcnow_ts() - rt.limited_at)
                limited_for = _fmt_duration(secs)
            out.append(
                PolicyState(
                    name=cfg.name,
                    enabled=cfg.enabled,
                    align=cfg.align,
                    window=cfg.window,
                    window_start=start.timestamp(),
                    window_end=end.timestamp(),
                    window_label=f"{start:%m-%d %H:%M} → {end:%m-%d %H:%M}",
                    threshold_total=cfg.threshold_total,
                    limit_total=cfg.limit_total,
                    clients=cfg.clients,
                    limited=rt.limited,
                    limited_at=rt.limited_at,
                    limited_for=limited_for,
                    current_total=total,
                    progress_total=round(progress, 2),
                    last_error="",
                    cooldown=cfg.cooldown,
                    resume_on_window_reset=cfg.resume_on_window_reset,
                )
            )
        return out

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._events[-limit:][::-1]

    # ------------------------------------------------------------ 求值
    async def evaluate(
        self,
        snapshots: dict[str, dict[str, Any]],
        clients: dict[str, Any],
    ) -> None:
        """用一轮采集结果求值所有策略。

        :param snapshots: {客户端名: 采集快照 dict}
        :param clients: {客户端名: BaseClient 实例}，用于下发限速
        """
        now = datetime.now()

        # 先处理周期切换与基线
        for rt in self._runtimes.values():
            cfg = rt.config
            start, _end = window_bounds(
                cfg.align, parse_duration(cfg.window), now
            )
            key = start.isoformat()

            if rt.window_key != key:
                # 新周期
                if rt.limited and cfg.resume_on_window_reset:
                    await self._resume(rt, clients, reason="进入新周期")
                rt.window_key = key
                rt.baseline = {
                    name: int(snap.get("up_total") or 0)
                    for name, snap in snapshots.items()
                    if cfg.applies_to(name)
                }
                logger.info("策略「%s」进入新周期 %s", cfg.name, key)
                continue

            # 更新基线：累计量回退（客户端重启）时重新对齐
            for name in list(rt.baseline):
                snap = snapshots.get(name)
                if not snap:
                    continue
                current = int(snap.get("up_total") or 0)
                if current < rt.baseline[name]:
                    logger.info(
                        "客户端 %s 累计量回退（%d → %d），重置基线",
                        name, rt.baseline[name], current,
                    )
                    rt.baseline[name] = current

        # 再判断是否需要限速
        for rt in self._runtimes.values():
            cfg = rt.config
            if not cfg.enabled or cfg.threshold_total <= 0:
                continue

            # 统计本周期内增量
            used = 0
            for name, base in rt.baseline.items():
                snap = snapshots.get(name) or {}
                current = int(snap.get("up_total") or 0)
                used += max(0, current - base)

            rt.used = used

            if rt.limited:
                continue

            if used >= cfg.threshold_total:
                await self._limit(rt, clients, used)

    async def _limit(
        self, rt: PolicyRuntime, clients: dict[str, Any], used: int
    ) -> None:
        """下发限速。"""
        cfg = rt.config
        targets = [
            name for name in rt.baseline
            if cfg.applies_to(name) and name in clients
        ]
        if not targets:
            return

        success = []
        errors = []
        for name in targets:
            client = clients[name]
            try:
                # 记录原始限速以便恢复
                before = await self._current_limit(client)
                rt.original_limits[name] = before
                await client.set_upload_limit(cfg.limit_total)
                success.append(name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")

        if success:
            rt.limited = True
            rt.limited_at = utcnow_ts()
            self._add_event(
                "limit",
                cfg.name,
                success,
                f"策略「{cfg.name}」上传量达阈值，已限速",
                used=used,
                threshold=cfg.threshold_total,
                old_limit=0,
                new_limit=cfg.limit_total,
            )
            logger.warning(
                "策略「%s」触发限速：已用 %s / 阈值 %s，限速至 %s/s",
                cfg.name, format_bytes(used),
                format_bytes(cfg.threshold_total),
                format_bytes(cfg.limit_total),
            )
        if errors:
            logger.error("策略「%s」限速失败: %s", cfg.name, "; ".join(errors))

    async def _resume(
        self, rt: PolicyRuntime, clients: dict[str, Any], reason: str = ""
    ) -> None:
        """解除限速。"""
        cfg = rt.config
        if not rt.limited:
            return

        targets = [n for n in rt.original_limits if n in clients]
        success = []
        for name in targets:
            try:
                # 优先用策略指定的恢复速度，否则回到原始限速
                if cfg.resume_limit_total > 0:
                    target = cfg.resume_limit_total
                else:
                    target = rt.original_limits.get(name, 0)
                await clients[name].set_upload_limit(target)
                success.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.error("解除限速失败 %s: %s", name, exc)

        rt.limited = False
        rt.limited_at = 0.0
        if success:
            self._add_event(
                "resume",
                cfg.name,
                success,
                f"策略「{cfg.name}」已解除限速（{reason}）",
            )
            logger.info("策略「%s」解除限速（%s）", cfg.name, reason)

    async def _current_limit(self, client: Any) -> int:
        """尽量读取当前限速；不支持则返回 0。"""
        getter = getattr(client, "get_upload_limit", None)
        if not callable(getter):
            return 0
        try:
            return int(await getter())
        except Exception:  # noqa: BLE001
            return 0

    def _add_event(self, etype: str, policy: str, clients: list[str], message: str, **extra: Any) -> None:
        self._events.append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "type": etype,
                "policy": policy,
                "clients": clients,
                "message": message,
                **extra,
            }
        )
        # 有界，避免长期运行无限增长
        if len(self._events) > 500:
            self._events = self._events[-300:]

    # ------------------------------------------------------------ 手动操作
    async def force_resume(self, name: str, clients: dict[str, Any]) -> bool:
        rt = self._runtimes.get(name)
        if rt is None:
            raise ValueError(f"未找到策略 {name}")
        if not rt.limited:
            return False
        await self._resume(rt, clients, reason="手动解除")
        return True

    async def force_trigger(self, name: str, clients: dict[str, Any]) -> bool:
        rt = self._runtimes.get(name)
        if rt is None:
            raise ValueError(f"未找到策略 {name}")
        if rt.limited:
            return False
        await self._limit(rt, clients, rt.used)
        return True


def _fmt_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs} 秒"
    if secs < 3600:
        return f"{secs // 60} 分钟"
    if secs < 86400:
        return f"{secs // 3600} 小时 {secs % 3600 // 60} 分"
    return f"{secs // 86400} 天 {secs % 86400 // 3600} 时"
