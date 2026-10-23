"""核心监控调度引擎。

两套独立任务，跑在 APScheduler 的 AsyncIOScheduler 上：

1. ``poll_cd2_uploads``（默认每 30 秒）—— CD2 守卫
   - 拉取 CD2 全量上传任务
   - **作用域过滤**：只有落在用户配置的源文件夹内的任务才参与判断
   - **判定依据：有没有上传速度**（两轮之间字节数是否增长）
       * 有速度 → 计入待观察名单
       * 没速度 → 秒传或排队，不入名单
   - **连续两次出现在名单上** → 判定无法秒传 → 自动取消上传
   - 重试交由 CD2 的备份任务自行处理，本系统不再调度

2. ``reap_p115_offline``（默认每 300 秒）
   - 拉取 115 云下载列表
   - ``now - add_time > 阈值`` 且未完成 → 删除离线任务（不删源文件）
   - 写 p115_cancel_logs
"""

from __future__ import annotations

from typing import Any

try:  # APScheduler 在运行环境是硬依赖；测试环境缺失时不影响规则匹配逻辑被导入
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    _APSCHEDULER_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore[assignment,misc]
    IntervalTrigger = None  # type: ignore[assignment,misc]
    _APSCHEDULER_AVAILABLE = False

from .. import config as C
from ..models import (
    NonInstantTask,
    P115CancelLog,
    P115OfflineTask,
    Rule,
    get_setting_bool,
    get_setting_float,
    get_setting_int,
    get_settings_map,
    session_scope,
    trim_event_logs,
)
from ..utils import get_logger, log_event, utcnow_ts
from .cd2_client import CD2AuthError, CD2Client, CD2Error, UploadTask, get_cd2_client
from .p115_client import (
    OfflineTask,
    P115AuthExpired,
    P115ClientWrapper,
    P115Error,
    get_p115_client,
)
from .webhook import enrich_tasks

logger = get_logger("guard.scheduler")


# ===========================================================================
# 规则匹配
# ===========================================================================
# 匹配逻辑已抽到 services/rules.py（纯函数，可脱离 DB 单元测试），
# 此处仅做再导出，保持调度器内部调用点不变。
from .rules import (  # noqa: E402
    MatchResult,
    build_matcher as _build_matcher,
    compile_keyword as _compile_keyword,
    match_rule,
)


# ===========================================================================
# 调度器
# ===========================================================================
class GuardScheduler:
    """监控与重试调度引擎。"""

    def __init__(self) -> None:
        if not _APSCHEDULER_AVAILABLE:
            raise RuntimeError(
                "APScheduler 未安装，无法启动调度引擎（pip install APScheduler）"
            )
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.cd2: CD2Client = get_cd2_client()
        self.p115: P115ClientWrapper = get_p115_client()
        self._started = False
        # 运行时统计（供仪表盘展示，不落库）
        self.stats: dict[str, Any] = {
            "cd2_last_run": 0.0,
            "cd2_last_ok": False,
            "cd2_last_error": "",
            "cd2_tasks_seen": 0,
            "p115_last_run": 0.0,
            "p115_last_ok": False,
            "p115_last_error": "",
            "p115_tasks_seen": 0,
            "retry_last_run": 0.0,
        }

    async def start(self) -> None:
        """加载配置并注册所有定时任务。"""
        if self._started:
            return
        await self.reload_config()

        # CD2 守卫轮询：30 秒一轮。
        #
        # 这个间隔是「连续两次观察到上传速度」判定的时间基准：
        # 两次轮询约 60 秒，足以区分「真的在传数据」和「秒传/排队」。
        # 太短会给 CD2 造成压力，太长则拦截不够及时。
        self.scheduler.add_job(
            self.poll_cd2_uploads,
            trigger=IntervalTrigger(seconds=30),
            id="poll_cd2_uploads",
            name="CD2 上传任务轮询",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=10,
        )
        self.scheduler.add_job(
            self.reap_p115_offline,
            trigger=IntervalTrigger(seconds=300),
            id="reap_p115_offline",
            name="115 云下载超时清理",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        # 连接健康检查：每 60 秒一次，且**启动时立即执行**。
        # 仪表盘的状态由它维护，用户不再需要手动点「测试连接」。
        self.scheduler.add_job(
            self.health_check,
            trigger=IntervalTrigger(seconds=60),
            id="health_check",
            name="连接健康检查",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        # PT 流量采集与策略求值：每 30 秒一轮
        self.scheduler.add_job(
            self.poll_pt_traffic,
            trigger=IntervalTrigger(seconds=30),
            id="poll_pt_traffic",
            name="PT 流量采集",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=20,
        )
        self.scheduler.add_job(
            self.housekeeping,
            trigger=IntervalTrigger(hours=6),
            id="housekeeping",
            name="数据清理",
            max_instances=1,
            coalesce=True,
        )

        self.scheduler.start()
        self._started = True
        logger.info("调度器已启动")

        # 立即做一次健康检查，让仪表盘在首屏就有正确状态
        try:
            await self.health_check()
            logger.info("启动时健康检查已完成")
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动时健康检查失败（不影响启动）: %s", exc)

    async def shutdown(self) -> None:
        """停止调度器并关闭连接。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.cd2.close()
        await self.p115.close()
        self._started = False
        logger.info("调度器已停止")

    async def reload_config(self) -> None:
        """从数据库重新加载配置（WebUI 改配置后调用）。

        同时把轮询间隔应用到已在运行的任务上。
        """
        with session_scope() as session:
            settings = get_settings_map(session)

        cd2_host = settings.get("cd2.host", "")
        await self.cd2.configure(
            host=cd2_host,
            token=settings.get("cd2.token", ""),
            username=settings.get("cd2.username", ""),
            password=settings.get("cd2.password", ""),
        )
        await self.p115.configure(settings.get("p115.cookie", ""))

        # ---- 应用到已注册的任务 ----
        cd2_interval = max(2, get_setting_int(settings, "cd2.poll_interval", 3))
        p115_interval = max(
            30, get_setting_int(settings, "p115.poll_interval", 300)
        )
        for job_id, seconds in (
            ("poll_cd2_uploads", cd2_interval),
            ("reap_p115_offline", p115_interval),
        ):
            if self.scheduler.running and self.scheduler.get_job(job_id):
                try:
                    self.scheduler.reschedule_job(
                        job_id, trigger=IntervalTrigger(seconds=seconds)
                    )
                    logger.info("任务 %s 间隔已调整为 %ds", job_id, seconds)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("调整任务 %s 间隔失败: %s", job_id, exc)

        self._settings_cache = settings
        logger.info("配置已重新加载（CD2=%s）", cd2_host)

    # ======================================================================
    # CD2 上传任务轮询（核心状态机）
    # ======================================================================
    async def poll_cd2_uploads(self) -> None:
        """单轮 CD2 上传任务扫描。"""
        now = utcnow_ts()
        self.stats["cd2_last_run"] = now

        with session_scope() as session:
            settings = get_settings_map(session)
            rules = list(
                session.query(Rule)
                .filter(Rule.enabled.is_(True))
                .order_by(Rule.priority.asc(), Rule.id.asc())
                .all()
            )

        if not self.cd2.status.configured and not self.cd2.host:
            self.stats["cd2_last_ok"] = False
            self.stats["cd2_last_error"] = "未配置 CD2 地址"
            return

        # ---- 拉取任务 ----
        try:
            tasks = await self.cd2.list_upload_tasks()
        except CD2AuthError as exc:
            self.stats["cd2_last_ok"] = False
            self.stats["cd2_last_error"] = str(exc)
            # 鉴权失败说明连通但 Token 无效
            self.cd2.mark_status(
                connected=True, authenticated=False, last_error=str(exc)
            )
            log_event("error", "cd2", "CD2 鉴权失败", detail=str(exc))
            return
        except CD2Error as exc:
            self.stats["cd2_last_ok"] = False
            self.stats["cd2_last_error"] = str(exc)
            self.cd2.mark_status(
                connected=False, authenticated=False, last_error=str(exc)
            )
            logger.warning("拉取 CD2 上传任务失败: %s", exc)
            return

        self.stats["cd2_last_ok"] = True
        self.stats["cd2_last_error"] = ""
        self.stats["cd2_tasks_seen"] = len(tasks)
        # 轮询成功即证明「连通 + 鉴权」都正常，同步给仪表盘
        self.cd2.mark_status(
            connected=True, authenticated=True, last_error=""
        )

        # ---- 用 webhook 映射补全真实源路径（弥补 gRPC 无源路径的缺陷）----
        # 失败不影响主流程：匹配会回退到 destPath 推导
        patched = 0
        try:
            patched = enrich_tasks(tasks)
        except Exception as exc:  # noqa: BLE001
            logger.debug("webhook 源路径补全失败（忽略）: %s", exc)
        if patched:
            logger.debug("已用 webhook 映射补全 %d 个任务的源路径", patched)

        with session_scope() as session:
            await self._process_upload_tasks(session, tasks, rules, now)

    async def _process_upload_tasks(
        self,
        session,
        tasks: list[UploadTask],
        rules: list[Rule],
        now: float,
    ) -> None:
        """处理单个轮次的上传任务。

        新版逻辑（v4）：

        1. 只要落在用户配置的源文件夹内的任务才参与判断
        2. 用两轮之间的字节增量判断「有没有上传速度」
        3. 连续两次有速度 → 判定无法秒传 → 取消上传
        4. 重试交给 CD2 自己处理，本系统不再调度

        相比旧版删掉了：状态机快照、超时计时、秒传判定、
        重试队列推进 —— 那些都在「连续两次有速度」这一条规则下变得多余。
        """
        from .cd2_guard import get_cd2_guard

        guard = get_cd2_guard()

        # ---- 1. 收集用户配置的源文件夹 ----
        folders: list[str] = []
        for r in rules:
            for line in (r.source_keywords or "").splitlines():
                line = line.strip()
                if line:
                    folders.append(line)

        if not folders:
            # 没有配置任何文件夹 → 不监控任何任务，并清空观察名单
            if guard.watching_count:
                guard.reset()
            return

        # ---- 2. 更新待观察名单 ----
        result = guard.observe(
            tasks,
            folders,
            now=now,
            cancel_threshold=C.CD2_CANCEL_AFTER_HITS,
        )

        # ---- 3. 取消判定为「无法秒传」的任务 ----
        if not result.to_cancel:
            return

        keys = [w.task_key for w in result.to_cancel]
        try:
            cancelled = await self.cd2.cancel_uploads(keys)
        except CD2Error as exc:
            logger.warning("取消上传失败: %s", exc)
            log_event(
                "error", "cd2", "取消上传失败", detail=str(exc)[:500]
            )
            return

        # ---- 4. 落库 + 记录事件 ----
        for w in result.to_cancel:
            guard.clear(w.task_key)
            row = NonInstantTask(
                task_key=w.task_key,
                file_name=w.file_name,
                source_path=w.source_path,
                dest_path=w.dest_path,
                size=w.size,
                rule_name="源文件夹监控",
                # 不重试：CD2 备份任务会自行重新扫描
                status=C.STATUS_CANCELLED,
                retry_count=0,
                max_retry=0,
                first_seen_at=w.first_seen_at,
                intercepted_at=now,
                finished_at=now,
                error_message="",
            )
            session.add(row)
            logger.info(
                "已取消无法秒传的上传: %s（连续 %d 次检测到传输，%.1f KB/s）",
                w.file_name,
                w.hits,
                w.last_speed / 1024,
            )
            log_event(
                "warning",
                "cd2",
                f"已取消无法秒传的上传: {w.file_name}",
                detail=(
                    f"source={w.source_path} speed={int(w.last_speed)}B/s "
                    f"hits={w.hits} size={w.size}"
                ),
            )

        session.flush()
        if cancelled != len(keys):
            logger.warning(
                "本轮请求取消 %d 个，CD2 确认 %d 个", len(keys), cancelled
            )

    def watching_snapshot(self) -> list[dict]:
        """当前待观察名单（供 API 展示）。"""
        from .cd2_guard import get_cd2_guard

        return get_cd2_guard().snapshot()

    async def reap_p115_offline(self) -> None:
        """拉取云下载列表，超过阈值的未完成任务自动取消。"""
        now = utcnow_ts()
        self.stats["p115_last_run"] = now

        with session_scope() as session:
            settings = get_settings_map(session)

        threshold = int(
            get_setting_float(settings, "p115.timeout_threshold", 86400)
        )
        auto_cancel = get_setting_bool(settings, "p115.auto_cancel", True)
        page_size = max(10, get_setting_int(settings, "p115.task_page_size", 30))

        if not self.p115.configured:
            self.stats["p115_last_ok"] = False
            self.stats["p115_last_error"] = "未配置 115 Cookie"
            return

        try:
            # 先拉「进行中」的任务，再补充全部。
            #
            # 为什么分两次：115 的「全部」列表按添加时间倒序，完成记录
            # 会大量堆积（实测几百上千条），进行中的任务很容易被挤到
            # 后面几页甚至翻不到。分两次拉能保证「下载中」的一定被看到
            # —— 而超时清理恰恰只关心这部分。
            running: list = []
            try:
                running = await self.p115.list_offline_tasks(
                    page_size=page_size, stat=C.P115_STAT_RUNNING
                )
            except P115Error as exc:
                logger.debug("拉取「进行中」任务失败（回退到全部）: %s", exc)

            tasks = await self.p115.list_offline_tasks(page_size=page_size)

            # 合并去重：以 info_hash 为键，进行中的优先保留
            merged: dict[str, object] = {}
            for t in tasks:
                merged[t.info_hash] = t
            for t in running:
                merged[t.info_hash] = t
            tasks = list(merged.values())
        except P115AuthExpired as exc:
            self.stats["p115_last_ok"] = False
            self.stats["p115_last_error"] = str(exc)
            log_event("error", "p115", "115 Cookie 失效", detail=str(exc))
            return
        except P115Error as exc:
            self.stats["p115_last_ok"] = False
            self.stats["p115_last_error"] = str(exc)
            logger.warning("拉取 115 云下载列表失败: %s", exc)
            return

        self.stats["p115_last_ok"] = True
        self.stats["p115_last_error"] = ""
        self.stats["p115_tasks_seen"] = len(tasks)

        # ---- 更新快照 ----
        with session_scope() as session:
            self._sync_offline_snapshots(session, tasks, now, threshold)

        # ---- 筛选超时任务 ----
        expired = [
            t for t in tasks
            if not t.is_terminal and t.age_seconds(now) > threshold
        ]
        if not expired:
            return

        if not auto_cancel:
            logger.info("发现 %d 个超时云下载任务，但自动取消已关闭", len(expired))
            return

        logger.info(
            "发现 %d 个超时云下载任务（阈值 %ds），开始取消", len(expired), threshold
        )

        removed_ok: list[str] = []
        for task in expired:
            try:
                ok, message = await self.p115.remove_offline_task(task.info_hash)
            except P115AuthExpired as exc:
                log_event("error", "p115", "取消云下载时 Cookie 失效", detail=str(exc))
                self.stats["p115_last_ok"] = False
                self.stats["p115_last_error"] = str(exc)
                return
            except P115Error as exc:
                ok, message = False, str(exc)

            self._write_cancel_log(task, now, threshold, ok, message, "auto")
            if ok:
                removed_ok.append(task.info_hash)

        # ---- 标记已移除 ----
        if removed_ok:
            with session_scope() as session:
                for info_hash in removed_ok:
                    row = (
                        session.query(P115OfflineTask)
                        .filter(P115OfflineTask.info_hash == info_hash)
                        .one_or_none()
                    )
                    if row is not None:
                        row.normalized_status = C.P115_ST_CANCELLED
                        row.last_seen_at = now

    def _sync_offline_snapshots(
        self,
        session,
        tasks: list[OfflineTask],
        now: float,
        threshold: int,
    ) -> None:
        """把 115 列表同步到本地快照表。"""
        existing = {
            row.info_hash: row
            for row in session.query(P115OfflineTask).all()
        }
        seen: set[str] = set()

        for task in tasks:
            seen.add(task.info_hash)
            status = task.normalized_status
            row = existing.get(task.info_hash)

            if row is None:
                session.add(
                    P115OfflineTask(
                        info_hash=task.info_hash,
                        name=task.name,
                        size=task.size,
                        add_time=task.add_time,
                        status=task.status,
                        percent_done=task.percent_done,
                        normalized_status=status,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                row.name = task.name or row.name
                row.size = task.size or row.size
                row.add_time = task.add_time or row.add_time
                row.status = task.status
                row.percent_done = task.percent_done
                # 已被我们取消的任务不再被 115 状态覆盖回“进行中”
                if row.normalized_status != C.P115_ST_CANCELLED:
                    row.normalized_status = status
                row.last_seen_at = now

        # 列表里消失的任务标记 removed
        for info_hash, row in existing.items():
            if info_hash in seen:
                continue
            if row.normalized_status in (
                C.P115_ST_CANCELLED,
                C.P115_ST_REMOVED,
            ):
                continue
            if now - row.last_seen_at > 3600:
                row.normalized_status = C.P115_ST_REMOVED

    def _write_cancel_log(
        self,
        task: OfflineTask,
        now: float,
        threshold: int,
        success: bool,
        message: str,
        trigger: str,
    ) -> None:
        """写取消日志。"""
        with session_scope() as session:
            session.add(
                P115CancelLog(
                    info_hash=task.info_hash,
                    name=task.name,
                    add_time=task.add_time,
                    cancel_at=now,
                    percent_done=task.percent_done,
                    status=task.status,
                    age_sec=task.age_seconds(now),
                    threshold_sec=threshold,
                    success=success,
                    message=message,
                    trigger=trigger,
                )
            )
        if success:
            logger.info(
                "已取消云下载: %s（%.0f%%，存在 %.1fh）",
                task.name, task.percent_done, task.age_seconds(now) / 3600,
            )
        else:
            log_event(
                "error",
                "p115",
                f"取消云下载失败: {task.name}",
                detail=f"info_hash={task.info_hash} msg={message}",
            )

    # ======================================================================
    # 数据清理
    # ======================================================================
    async def housekeeping(self) -> None:
        """裁剪事件日志、清理已消失的旧快照。"""
        try:
            with session_scope() as session:
                removed = trim_event_logs(session, C.MAX_EVENT_LOGS)
                if removed:
                    logger.info("已裁剪 %d 条事件日志", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("数据清理失败: %s", exc)

    # ======================================================================
    # PT 流量采集
    # ======================================================================
    async def poll_pt_traffic(self) -> None:
        """采集一轮 PT 流量并求值限速策略。

        原先这里是对 Go 子进程的存活看护；整合后完全由本进程执行：

        1. 并发采集所有已配置下载器的速率与累计上传量
        2. 用策略引擎判断是否达到阈值 → 下发限速
        3. 把策略事件汇入统一事件日志
        """
        try:
            from .bt_monitor import get_pt_monitor

            monitor = get_pt_monitor()
            if not monitor.client_names and not monitor.engine.configs():
                return
            await monitor.poll_once()
            self._import_pt_events(monitor)
        except Exception as exc:  # noqa: BLE001 - 采集失败不影响主流程
            logger.debug("PT 采集异常: %s", exc)

    def _import_pt_events(self, monitor) -> None:
        """把新增的策略事件写入统一事件日志（按时间去重）。"""
        try:
            events = monitor.events(50)
        except Exception:  # noqa: BLE001
            return
        if not events:
            return

        if not hasattr(self, "_pt_seen_events"):
            self._pt_seen_events: set[str] = set()
        seen = self._pt_seen_events

        for ev in reversed(events):
            key = "{}|{}|{}".format(
                ev.get("time", ""), ev.get("type", ""), ev.get("policy", "")
            )
            if key in seen:
                continue
            seen.add(key)

            etype = str(ev.get("type", ""))
            message = str(ev.get("message", "")) or f"PT 策略事件: {etype}"
            level = "info"
            if etype == "limit":
                level = "warning"
            elif "error" in etype or "fail" in etype:
                level = "error"

            detail = (
                f"policy={ev.get('policy', '')} "
                f"clients={','.join(ev.get('clients') or [])} "
                f"used={ev.get('used', 0)} "
                f"threshold={ev.get('threshold', 0)} "
                f"limit={ev.get('new_limit', 0)}"
            )
            log_event(level, "pt-traffic", message, detail=detail)

        if len(seen) > 2000:
            self._pt_seen_events = set(list(seen)[-1000:])

    async def health_check(self) -> None:
        """定期探测 CD2 与 115 的连通性，并更新仪表盘用的状态。

        **为什么需要这个任务**

        仪表盘读的是 ``cd2.status.connected`` / ``p115.status.logged_in``，
        而这两个字段原先只在用户**手动点击「连接测试」**时才被赋值。
        结果：容器重启后仪表盘一直显示「失败 / 未验证」，
        即使服务其实完全正常，必须手动点一次测试才恢复 —— 这正是
        用户反馈的现象。

        现在改为：启动时立即探一次，之后每 60 秒探一次，
        状态由后台维护，无需人工干预。
        """
        await self._check_cd2()
        await self._check_p115()

    async def _check_cd2(self) -> None:
        """探测 CD2 连通性与鉴权状态。"""
        self.stats["cd2_health_run"] = utcnow_ts()

        if not self.cd2.host:
            self.cd2.mark_status(
                connected=False,
                authenticated=False,
                last_error="未配置 CD2 地址",
            )
            return

        try:
            info = await self.cd2.get_system_info()
        except CD2Error as exc:
            self.cd2.mark_status(
                connected=False, authenticated=False, last_error=str(exc)
            )
            self.stats["cd2_health_ok"] = False
            logger.debug("CD2 健康检查失败: %s", exc)
            return

        # gRPC 通了；再确认 Token 能否真正读数据
        authenticated = False
        upload_count = 0
        auth_error = ""
        if self.cd2._token:  # noqa: SLF001 - 同模块内的状态查询
            try:
                counts = await self.cd2.get_all_tasks_count()
                authenticated = True
                upload_count = int(counts.get("upload_count", 0))
            except CD2AuthError as exc:
                auth_error = str(exc)
            except CD2Error as exc:
                auth_error = str(exc)

        self.cd2.mark_status(
            connected=True,
            authenticated=authenticated,
            user_name=info.get("user_name", "") or "",
            upload_count=upload_count,
            message=info.get("system_message", "") or "",
            last_error=auth_error,
        )
        self.stats["cd2_health_ok"] = True
        logger.debug(
            "CD2 健康检查: 连通=True 鉴权=%s 上传队列=%d",
            authenticated, upload_count,
        )

    async def _check_p115(self) -> None:
        """探测 115 Cookie 是否仍有效。"""
        self.stats["p115_health_run"] = utcnow_ts()

        if not self.p115.configured:
            self.p115.mark_status(logged_in=False, last_error="未配置 115 Cookie")
            return

        try:
            result = await self.p115.check_login()
        except Exception as exc:  # noqa: BLE001 - 健康检查绝不抛异常
            self.p115.mark_status(logged_in=False, last_error=str(exc))
            self.stats["p115_health_ok"] = False
            return

        self.stats["p115_health_ok"] = bool(result.get("ok"))
        if not result.get("ok"):
            logger.debug("115 健康检查失败: %s", result.get("message"))

    # ======================================================================
    # 供 API 调用的即时动作
    # ======================================================================
    async def manual_retry(self, row_ids: list[int]) -> dict[str, Any]:
        """手动立即重试指定记录（触发备份重新扫描）。"""
        now = utcnow_ts()
        result: dict[str, Any] = {"triggered": 0, "failed": 0, "details": []}

        with session_scope() as session:
            rows = (
                session.query(NonInstantTask)
                .filter(NonInstantTask.id.in_(row_ids))
                .all()
            )

            for row in rows:
                if row.status in (C.STATUS_SUCCESS, C.STATUS_CANCELLED):
                    continue
                if row.retry_count >= row.max_retry:
                    result["failed"] += 1
                    result["details"].append(
                        f"{row.file_name}: 已达最大重试次数"
                    )
                    continue

                try:
                    ok = await self._retrigger(
                        row.file_name, row.source_path, row.dest_path
                    )
                except CD2Error as exc:
                    logger.warning("手动重试失败 %s: %s", row.file_name, exc)
                    ok = False

                row.retry_count += 1
                row.last_retry_at = now
                if ok:
                    row.status = C.STATUS_RETRYING
                    row.attempt_seen_at = now
                    row.next_retry_at = None
                    result["triggered"] += 1
                    result["details"].append(f"{row.file_name}: 已触发备份重扫")
                else:
                    row.status = C.STATUS_WAITING
                    row.next_retry_at = now + row.retry_interval
                    result["failed"] += 1
                    result["details"].append(
                        f"{row.file_name}: 重新触发失败，已排回队列"
                    )

        return result

    async def manual_cancel_retry(self, row_ids: list[int]) -> int:
        """手动取消重试（记录置为 cancelled）。"""
        now = utcnow_ts()
        count = 0
        with session_scope() as session:
            rows = (
                session.query(NonInstantTask)
                .filter(NonInstantTask.id.in_(row_ids))
                .all()
            )
            for row in rows:
                if row.status in (C.STATUS_SUCCESS, C.STATUS_CANCELLED):
                    continue
                row.status = C.STATUS_CANCELLED
                row.next_retry_at = None
                row.finished_at = now
                row.error_message = "用户手动取消重试"
                count += 1
        return count

    async def manual_cancel_offline(
        self, info_hashes: list[str]
    ) -> dict[str, Any]:
        """手动取消指定云下载任务。"""
        now = utcnow_ts()
        threshold = 0
        with session_scope() as session:
            settings = get_settings_map(session)
            threshold = int(
                get_setting_float(settings, "p115.timeout_threshold", 86400)
            )

        results = await self.p115.remove_offline_tasks(info_hashes)

        with session_scope() as session:
            snapshots = {
                row.info_hash: row
                for row in session.query(P115OfflineTask)
                .filter(P115OfflineTask.info_hash.in_(info_hashes))
                .all()
            }

        for info_hash, (ok, message) in results.items():
            snapshot = snapshots.get(info_hash)
            task = OfflineTask(
                info_hash=info_hash,
                name=snapshot.name if snapshot else "",
                size=snapshot.size if snapshot else 0,
                add_time=snapshot.add_time if snapshot else 0,
                status=snapshot.status if snapshot else 0,
                percent_done=snapshot.percent_done if snapshot else 0.0,
            )
            self._write_cancel_log(task, now, threshold, ok, message, "manual")
            if ok:
                with session_scope() as session:
                    row = (
                        session.query(P115OfflineTask)
                        .filter(P115OfflineTask.info_hash == info_hash)
                        .one_or_none()
                    )
                    if row is not None:
                        row.normalized_status = C.P115_ST_CANCELLED
                        row.last_seen_at = now

        return {
            "success": sum(1 for ok, _ in results.values() if ok),
            "failed": sum(1 for ok, _ in results.values() if not ok),
            "details": {k: v[1] for k, v in results.items()},
        }


# ===========================================================================
# 全局单例
# ===========================================================================
_scheduler: GuardScheduler | None = None


def get_scheduler() -> GuardScheduler:
    """获取进程内唯一调度引擎。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = GuardScheduler()
    return _scheduler
