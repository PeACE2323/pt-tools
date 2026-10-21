"""CD2 守卫：识别「无法秒传」的上传任务并自动取消。

## 设计（v4 重构）

旧逻辑用「传输耗时超过阈值」判定，受排队、并发、网络抖动影响大，
误判较多。新逻辑改为观察**传输行为是否持续**，简单且贴合秒传的本质：

    每 30 秒查询一次 CD2 上传列表
        ↓
    只看落在「用户指定的源文件夹」内的任务
        ↓
    判断该任务是否有上传速度
        ↓
    有速度 → 计入待观察名单，命中次数 +1
    没速度 → 从名单移除（秒传、排队中都属于这类）
        ↓
    同一个文件**连续两次**出现在待观察名单 → 判定无法秒传 → 自动取消

## 为什么用「连续两次有速度」而不是「耗时」

秒传的本质是「服务器已有该文件，不接收数据」，因此秒传任务**从不
产生上传流量**。反过来，只要一个任务在持续传输数据，就说明它没有
走秒传通道 —— 这个判断与排队时长、网络快慢都无关。

真正传数据的任务，两次轮询（约 60 秒）必然能观察到字节增长；
秒传任务在第一次轮询前就完成了，或者始终是 0 字节。

## 上传速度怎么算

CD2 的 ``UploadFileInfo`` **没有 speed 字段**，只能自己算：

    速度 = (本次 transferedBytes − 上次 transferedBytes) / 间隔

因此需要缓存上一轮的字节数。首次见到的任务没有对比基准，
先记下来但不算「有速度」—— 避免把已经传了一半的老任务误判。

## 重试交给 CD2

本系统只负责「取消」，不再自行调度重试。CD2 的备份任务本身会
重新扫描并重传缺失文件，重复实现一遍只会有两套状态互相打架。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cd2_client import UploadTask
from ..utils import get_logger, utcnow_ts

logger = get_logger("guard.cd2")


# ===========================================================================
# 待观察名单
# ===========================================================================
@dataclass
class WatchedTask:
    """一个「有上传速度」的观察对象。"""

    task_key: str
    file_name: str
    source_path: str
    dest_path: str
    size: int
    # 上一次观测到的传输字节数，用于计算速度
    last_bytes: int
    # 首次进入观察名单的时间
    first_seen_at: float
    # 连续命中次数：达到 2 即判定无法秒传
    hits: int = 1
    # 最近一次观测到的瞬时速度（字节/秒），仅用于展示
    last_speed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_key": self.task_key,
            "file_name": self.file_name,
            "source_path": self.source_path,
            "dest_path": self.dest_path,
            "size": self.size,
            "hits": self.hits,
            "speed": int(self.last_speed),
            "first_seen_at": self.first_seen_at,
            "observed_sec": max(0.0, utcnow_ts() - self.first_seen_at),
        }


@dataclass
class WatchResult:
    """一轮观察的结果。"""

    # 本轮判定为「无法秒传」、需要取消的任务
    to_cancel: list[WatchedTask] = field(default_factory=list)
    # 仍在观察中的任务
    watching: list[WatchedTask] = field(default_factory=list)


# ===========================================================================
# 源文件夹匹配
# ===========================================================================
def normalize_folder(path: str) -> str:
    """把用户填写的文件夹规范化，便于前缀比较。"""
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return ""
    # 去掉末尾多余的斜杠，保留根路径 "/"
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def task_in_folders(task: UploadTask, folders: list[str]) -> bool:
    """判断任务是否落在受监控的源文件夹内。

    CD2 的 ``UploadFileInfo`` 不含源路径，只能用推导出的 source_path
    或 dest_path 做前缀匹配。两者任一命中即算命中 —— 宁可多监控，
    也不要漏掉真正在传的任务。
    """
    if not folders:
        return False

    candidates = [
        normalize_folder(task.source_path),
        normalize_folder(task.dest_path),
    ]
    candidates = [c for c in candidates if c]

    for folder in folders:
        f = normalize_folder(folder)
        if not f:
            continue
        for cand in candidates:
            # 精确相等，或以「文件夹/」开头（避免 /PT 命中 /PTX）
            if cand == f or cand.startswith(f + "/"):
                return True
    return False


# ===========================================================================
# 守卫
# ===========================================================================
class CD2Guard:
    """维护待观察名单，判定并选出需要取消的任务。

    这个类只做纯判断，不碰数据库、不发 RPC —— 便于单元测试。
    真正的取消动作与落库由调用方（scheduler）完成。
    """

    def __init__(self) -> None:
        #: task_key -> WatchedTask
        self._watching: dict[str, WatchedTask] = {}
        #: task_key -> 上一轮观测到的传输字节数
        #: （用于给首次进入观察的任务提供对比基准）
        self._last_bytes: dict[str, int] = {}
        #: 上一轮的时间戳，用于计算速度
        self._last_poll_ts: float = 0.0

    # ------------------------------------------------------------ 观察
    def observe(
        self,
        tasks: list[UploadTask],
        folders: list[str],
        now: float | None = None,
        cancel_threshold: int = 2,
    ) -> WatchResult:
        """更新待观察名单，返回本轮需要取消的任务。

        :param tasks: 本轮从 CD2 拉到的全部上传任务
        :param folders: 用户配置的源文件夹列表
        :param now: 当前时间戳（便于测试注入）
        :param cancel_threshold: 连续命中多少次判定为无法秒传
        """
        now = now if now is not None else utcnow_ts()
        interval = max(0.0, now - self._last_poll_ts) if self._last_poll_ts else 0.0
        self._last_poll_ts = now

        result = WatchResult()
        seen: set[str] = set()
        next_last_bytes: dict[str, int] = {}

        for task in tasks:
            if not task.key:
                continue
            if not task_in_folders(task, folders):
                continue

            seen.add(task.key)
            current = int(task.transferred_bytes or 0)
            previous = self._last_bytes.get(task.key)
            next_last_bytes[task.key] = current
            watched = self._watching.get(task.key)

            # ---- 判断这一轮是否有上传速度 ----
            #
            # 两种情况都算「有上传」：
            #   a) 和上一轮比字节数增长了  → 明确在传数据
            #   b) 首次见到但已经传过数据  → 正在传，只是我们首次观测
            #
            # 秒传任务从头到尾都是 0 字节，因此永远不会命中这两种情况，
            # 也就不会被误取消。排队中的任务字节数同样为 0，也不会进名单。
            has_speed = False
            speed = 0.0
            if previous is not None:
                delta = current - previous
                if delta > 0:
                    has_speed = True
                    speed = delta / interval if interval > 0 else float(delta)
                elif watched is not None:
                    # 之前进过名单，这一轮字节没动 —— 可能卡住了。
                    # 不取消，但保留在名单里等下一轮；若持续不动会被下面清理。
                    has_speed = False
            else:
                # 首轮（无对比基准）：已传过数据即视为正在上传。
                if current > 0:
                    has_speed = True
                    speed = 0.0

            if has_speed:
                if watched is None:
                    watched = WatchedTask(
                        task_key=task.key,
                        file_name=task.file_name,
                        source_path=task.source_path,
                        dest_path=task.dest_path,
                        size=int(task.size or 0),
                        last_bytes=current,
                        first_seen_at=now,
                        hits=1,
                        last_speed=speed,
                    )
                    self._watching[task.key] = watched
                else:
                    watched.hits += 1
                    if speed:
                        watched.last_speed = speed
                    watched.last_bytes = current
                    watched.size = int(task.size or 0)
                    watched.file_name = task.file_name
                    watched.source_path = task.source_path
                    watched.dest_path = task.dest_path

                if watched.hits >= cancel_threshold:
                    result.to_cancel.append(watched)
            elif current <= 0 and watched is None:
                # 从未传过数据（秒传 / 排队）：不入名单
                pass
            elif watched is not None and current <= 0:
                # 字节回退到 0（任务被重置）：移出名单
                self._watching.pop(task.key, None)

        # 已经不在 CD2 列表里的任务（完成/被取消）一并清掉
        for key in list(self._watching):
            if key not in seen:
                self._watching.pop(key, None)

        # 只保留本轮还存在的字节基准，避免字典无限增长
        self._last_bytes = next_last_bytes

        result.watching = list(self._watching.values())
        return result

    # ------------------------------------------------------------ 查询
    def snapshot(self) -> list[dict]:
        """当前观察名单（供前端展示）。"""
        return [w.to_dict() for w in self._watching.values()]

    def clear(self, task_key: str) -> None:
        """取消或处理完某个任务后，把它移出名单。"""
        self._watching.pop(task_key, None)
        self._last_bytes.pop(task_key, None)

    def reset(self) -> None:
        """配置变更或长时间暂停后重置全部状态。"""
        self._watching.clear()
        self._last_bytes.clear()
        self._last_poll_ts = 0.0

    @property
    def watching_count(self) -> int:
        return len(self._watching)


# ===========================================================================
# 全局单例
# ===========================================================================
_guard: CD2Guard | None = None


def get_cd2_guard() -> CD2Guard:
    global _guard
    if _guard is None:
        _guard = CD2Guard()
    return _guard
