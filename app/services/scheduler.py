"""核心监控与重试调度引擎。

两套独立任务，跑在 APScheduler 的 AsyncIOScheduler 上：

1. ``poll_cd2_uploads``（默认每 3 秒）
   - 拉取 CD2 全量上传任务
   - **作用域过滤**：只有命中监控规则的任务才会被记录与处理；
     未命中规则的任务（例如只监控含 "PT" 的源路径时的其它上传）
     完全不入库、不干预，避免污染统计与数据库
   - 维护 ``first_seen_at``（持久化在 upload_tasks 表，进程重启不丢）
   - 命中规则且**在阈值内完成** → 写 instant_uploads（秒传）
   - 命中规则且**上传耗时 > 阈值仍未完成** → 触发拦截 → 写 non_instant_tasks
   - 尝试重试后的任务若完成 → 标记 retry success

2. ``reap_p115_offline``（默认每 300 秒）
   - 拉取 115 云下载列表
   - ``now - add_time > 阈值`` 且未完成 → 删除离线任务（不删源文件）
   - 写 p115_cancel_logs

重试执行 ``process_retry_queue``（默认每 10 秒）：
   等待重试且到点 → pause_resume 策略调 ResumeUploadFiles；
   或 cancel_reupload 策略调 retrigger_upload。
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
    InstantUpload,
    NonInstantTask,
    P115CancelLog,
    P115OfflineTask,
    Rule,
    UploadTaskSnapshot,
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
from .webhook import enrich_tasks, lookup_source_path

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

    async def _retrigger(
        self, file_name: str, source_path: str, dest_path: str
    ) -> bool:
        """重新触发上传，优先使用 webhook 记录的真实源路径。

        源路径取值顺序：

        1. ``source_paths`` 表按文件名反查（webhook 写入，最可靠）；
        2. 记录里保存的 ``source_path``（可能是 destPath 推导值）；
        3. 都没有 → 直接失败。
        """
        resolved = source_path
        try:
            from_webhook = lookup_source_path(file_name)
            if from_webhook:
                resolved = from_webhook
                logger.info(
                    "使用 webhook 源路径重新触发: %s → %s",
                    from_webhook, dest_path,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("查询 webhook 源路径失败（忽略）: %s", exc)

        if not resolved or not dest_path:
            log_event(
                "warning",
                "scheduler",
                f"缺少源路径，无法重新触发: {file_name}",
                detail=f"source={resolved!r} dest={dest_path!r}",
            )
            return False

        return await self.cd2.retrigger_upload(resolved, dest_path)

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """加载配置并注册所有定时任务。"""
        if self._started:
            return
        await self.reload_config()

        self.scheduler.add_job(
            self.poll_cd2_uploads,
            trigger=IntervalTrigger(seconds=3),
            id="poll_cd2_uploads",
            name="CD2 上传任务轮询",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=10,
        )
        self.scheduler.add_job(
            self.process_retry_queue,
            trigger=IntervalTrigger(seconds=10),
            id="process_retry_queue",
            name="重试队列处理",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
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
        """处理单个轮次的上传任务（含状态机转移）。"""
        # ---- 1. 读取已有快照 ----
        existing = {
            row.task_key: row
            for row in session.query(UploadTaskSnapshot).all()
        }

        # 规则按 id 索引：任务消失时需要用其规则阈值判定秒传
        rules_by_id = {r.id: r for r in rules}

        # ---- 2. 读取活跃的重试记录（按 task_key 索引）----
        retry_rows = {
            row.task_key: row
            for row in session.query(NonInstantTask)
            .filter(NonInstantTask.status.in_(C.NON_INSTANT_ACTIVE_STATUSES))
            .all()
        }

        seen_keys: set[str] = set()
        # (命中的规则, 任务) —— 匹配结果随队列传递，避免二次查库
        intercept_queue: list[tuple[MatchResult, UploadTask]] = []
        # 本轮未被任何规则命中的 task_key：用于清理历史残留快照
        unmatched_keys: set[str] = set()

        for task in tasks:
            if not task.key:
                continue
            seen_keys.add(task.key)

            snapshot = existing.get(task.key)
            retry_row = retry_rows.get(task.key)

            is_finished = task.status_enum == C.CD2_STATUS_FINISH
            is_failed = task.status_enum in C.CD2_STATUS_FAILED

            # ---- 2.0 规则预匹配（作用域过滤）----
            # 功能约定：只有落在监控规则范围内的任务才需要被记录。
            # 例如只监控源路径含 "PT" 的上传任务，其余任务完全不入库、不干预。
            match = match_rule(task, rules)

            if match is None and retry_row is None:
                # 既没命中规则、也不在重试队列中 → 彻底忽略该任务
                unmatched_keys.add(task.key)
                if snapshot is not None:
                    # 规则被删除/禁用的历史残留：从实时表中移除
                    session.delete(snapshot)
                    existing.pop(task.key, None)
                continue

            # ---- 2.1 首次发现：创建快照 ----
            if snapshot is None:
                snapshot = UploadTaskSnapshot(
                    task_key=task.key,
                    file_name=task.file_name,
                    source_path=task.source_path,
                    dest_path=task.dest_path,
                    size=task.size,
                    transferred=task.transferred_bytes,
                    status_text=task.status_name,
                    status_enum=task.status_enum,
                    operator_type=task.operator_type,
                    first_seen_at=now,
                    last_seen_at=now,
                    state="active",
                    error_message=task.error_message,
                )
                if match is not None:
                    snapshot.matched_rule_id = match.rule_id
                    snapshot.matched_rule_name = match.rule_name
                session.add(snapshot)
                existing[task.key] = snapshot
            else:
                # ---- 2.2 已有快照：更新动态字段 ----
                snapshot.file_name = task.file_name
                snapshot.source_path = task.source_path
                snapshot.dest_path = task.dest_path
                snapshot.size = task.size
                snapshot.transferred = task.transferred_bytes
                snapshot.status_text = task.status_name
                snapshot.status_enum = task.status_enum
                snapshot.operator_type = task.operator_type
                snapshot.last_seen_at = now
                snapshot.error_message = task.error_message
                if match is not None:
                    snapshot.matched_rule_id = match.rule_id
                    snapshot.matched_rule_name = match.rule_name

            first_seen = snapshot.first_seen_at or now
            elapsed = max(0.0, now - first_seen)

            # ---- 2.3 重试中的任务：判定重试结果 ----
            if retry_row is not None:
                if is_finished:
                    retry_row.status = C.STATUS_SUCCESS
                    retry_row.finished_at = now
                    snapshot.state = "done"
                    log_event(
                        "info",
                        "scheduler",
                        f"重试成功: {retry_row.file_name}",
                        detail=(
                            f"task_key={task.key} "
                            f"retry={retry_row.retry_count}/{retry_row.max_retry} "
                            f"dest={task.dest_path}"
                        ),
                    )
                    retry_rows.pop(task.key, None)
                elif is_failed and retry_row.status == C.STATUS_RETRYING:
                    # 本轮重试失败，交给 process_retry_queue 决定下一轮
                    retry_row.status = C.STATUS_WAITING
                    retry_row.error_message = task.error_message or task.status_name
                    retry_row.next_retry_at = now + retry_row.retry_interval
                session.add(retry_row)
                continue

            # ---- 2.4 已完成的普通任务：判定秒传 ----
            if is_finished:
                snapshot.state = "done"
                # 命中的规则已在 2.0 写入快照；elapsed ≤ 阈值才算秒传
                threshold = match.timeout_threshold if match is not None else None
                self._record_instant_upload(
                    session, snapshot, elapsed, now, threshold
                )
                continue

            # ---- 2.5 已失败的任务：不拦截，只记录 ----
            if is_failed:
                snapshot.state = "done"
                continue

            # ---- 2.6 超时判定 ----
            if match is None:
                # 理论上到不了这里（2.0 已过滤），保留防御
                continue

            # 已进入 Pause 说明已被我们拦截过，不再重复计入超时
            if task.status_enum == C.CD2_STATUS_PAUSE:
                snapshot.state = "intercepted"
                continue

            # ★ 关键：只有 "Transfer"（真正在传数据）才计入上传超时。
            #
            # Preprocessing / Inqueue / WaitforPreprocessing 属于排队阶段，
            # CD2 有并发上限，任务排队几十分钟是正常的，不能算「不能秒传」。
            # 这里同时记录排队累计时长，供面板区分展示。
            if task.status_enum in C.CD2_STATUS_QUEUED:
                snapshot.state = "queued"
                # 累计排队时长（按轮询间隔累加，避免依赖 CD2 提供的字段）
                interval = max(0.0, now - (snapshot.last_seen_at or now))
                snapshot.queued_sec = float(snapshot.queued_sec or 0.0) + interval
                continue

            if task.status_enum in C.CD2_STATUS_TRANSFERRING:
                snapshot.state = "active"
                # 真正的上传耗时 = 总耗时 - 排队耗时，避免排队时间被算进阈值
                effective = max(0.0, elapsed - float(snapshot.queued_sec or 0.0))
                if effective > match.timeout_threshold:
                    intercept_queue.append((match, task))
                continue

        # ---- 3. 执行拦截（限制单轮数量，避免打爆 CD2）----
        if intercept_queue:
            await self._intercept_tasks(
                session, intercept_queue[: C.MAX_INTERCEPT_PER_ROUND], now
            )
            if len(intercept_queue) > C.MAX_INTERCEPT_PER_ROUND:
                logger.warning(
                    "本轮待拦截 %d 条，仅处理前 %d 条",
                    len(intercept_queue),
                    C.MAX_INTERCEPT_PER_ROUND,
                )

        # ---- 4. 处理从 CD2 列表中消失的任务 ----
        #
        # 这两类任务都可能消失：
        #   a) 正常完成 —— CD2 把已完成任务移出「上传任务」列表
        #   b) 秒传 —— 太快，在两次轮询之间就完成并消失了
        #
        # 秒传尤其可能「来不及看到 Finish 就消失」，若直接判 gone 就会
        # 漏记。这里补一条「消失即完成」的兜底判定。
        for key, snapshot in existing.items():
            if key in seen_keys:
                continue
            if snapshot.state in ("done", "gone"):
                continue

            # 消失满一个宽限期仍未再出现 —— 视为已结束
            if now - snapshot.last_seen_at > C.TASK_GONE_GRACE_SEC:
                rule = rules_by_id.get(snapshot.matched_rule_id) if snapshot.matched_rule_id else None
                completed = self._handle_task_vanished(session, snapshot, now, rule)
                snapshot.state = "done" if completed else "gone"

        if unmatched_keys:
            logger.debug(
                "本轮忽略 %d 个不在监控规则内的任务", len(unmatched_keys)
            )

    def _handle_task_vanished(
        self,
        session,
        snapshot: UploadTaskSnapshot,
        now: float,
        rule,
    ) -> bool:
        """任务从列表中消失时判定它是否属于「秒传完成」。

        判定依据：消失前最后一次观测到的 ``transferred`` 远小于 ``size``，
        说明数据从来没有真正传过 —— 这正是秒传的特征。

        :return: True 表示已按秒传记录处理，False 表示任务确实已离开。
        """
        size = int(snapshot.size or 0)
        transferred = int(snapshot.transferred or 0)

        if size <= 0:
            return False
        # 已经传了明显的数据量，说明是真实上传后结束（或失败），不记秒传
        if transferred / size >= 0.01:
            return False
        # 状态是明确的失败，不记秒传
        if snapshot.status_enum in C.CD2_STATUS_FAILED:
            return False
        # 从未被任何规则命中，不记
        if snapshot.matched_rule_id is None:
            return False

        elapsed = max(0.0, now - (snapshot.first_seen_at or now))
        logger.info(
            "任务消失且未传输数据，判定为秒传完成: %s（%d 字节 / 共 %d）",
            snapshot.file_name, transferred, size,
        )
        self._record_instant_upload(
            session,
            snapshot,
            elapsed,
            now,
            rule.timeout_threshold if rule is not None else None,
        )
        return True

    def _record_instant_upload(
        self,
        session,
        snapshot: UploadTaskSnapshot,
        elapsed: float,
        now: float,
        timeout_threshold: int | None = None,
    ) -> None:
        """写入秒传记录（同一 task_key 只写一次）。

        **秒传的判定依据是两个条件同时满足**：

        1. 文件最终完成了（调用方已确认 status == Finish）；
        2. **没有真正传输过文件数据** —— 即 ``transfered`` 远小于 ``size``。

        为什么以字节数为主、耗时为辅：

        秒传的本质是「服务器已有相同哈希的文件，因此不接收数据」，
        所以秒传任务从头到尾都不该有明显的数据传输。
        而耗时受排队、并发、CDN 抖动影响很大 —— 之前用「耗时 ≤ 阈值」
        判定，导致：
          * 排队久的秒传任务被误判成非秒传（记录为空）
          * 网络快时普通上传被误算成秒传

        阈值（timeout_threshold）保留为**兜底**：当 size 为 0 无法用
        字节判断时，才退回用耗时。
        """
        size = int(getattr(snapshot, "size", 0) or 0)
        transferred = int(getattr(snapshot, "transferred", 0) or 0)

        # ---- 主判据：是否真的传了数据 ----
        if size > 0:
            ratio = transferred / size
            if ratio >= 0.01:
                logger.info(
                    "完成但传输了 %.1f%% 数据（非秒传，不计入秒传记录）: %s",
                    ratio * 100, snapshot.file_name,
                )
                return
            # 通过字节判据 → 是秒传
            basis = "no-data-transfer"
        else:
            # ---- 兜底：无大小信息时用耗时 ----
            if timeout_threshold is not None and elapsed > timeout_threshold:
                logger.info(
                    "完成但超出阈值且无大小信息（不计秒传）: %s（%.1fs > %ds）",
                    snapshot.file_name, elapsed, timeout_threshold,
                )
                return
            basis = "duration-fallback"

        exists = (
            session.query(InstantUpload.id)
            .filter(InstantUpload.task_key == snapshot.task_key)
            .first()
        )
        if exists:
            return
        session.add(
            InstantUpload(
                task_key=snapshot.task_key,
                file_name=snapshot.file_name,
                source_path=snapshot.source_path,
                dest_path=snapshot.dest_path,
                size=snapshot.size,
                rule_id=snapshot.matched_rule_id,
                rule_name=snapshot.matched_rule_name,
                first_seen_at=snapshot.first_seen_at,
                finished_at=now,
                duration_sec=round(elapsed, 3),
                transferred_bytes=transferred,
            )
        )
        logger.info(
            "秒传: %s（大小 %d，实际传输 %d 字节，耗时 %.1fs，依据 %s）",
            snapshot.file_name, size, transferred, elapsed, basis,
        )

    async def _intercept_tasks(
        self,
        session,
        queue: list[tuple[MatchResult, UploadTask]],
        now: float,
    ) -> None:
        """对超时任务执行拦截动作，并写入重试队列。

        由于上传来自 CD2 备份任务，拦截动作统一为：
        **只取消该文件的上传任务**（不动整个备份，避免影响同一备份下的其他文件），
        到点后由 ``BackupRestartWalkingThrough`` 让备份重新扫描并重传。

        ``queue`` 中已携带匹配结果，无需二次查库；快照按 task_key 一次性取出。
        """
        if not queue:
            return

        keys_all = [task.key for _m, task in queue]
        snapshots = {
            row.task_key: row
            for row in session.query(UploadTaskSnapshot)
            .filter(UploadTaskSnapshot.task_key.in_(keys_all))
            .all()
        }

        # 只处理有快照的任务
        targets = [(m, t) for m, t in queue if t.key in snapshots]
        if not targets:
            return

        # ---- 执行 RPC：仅取消单个上传任务 ----
        keys = [task.key for _m, task in targets]
        succeeded: set[str] = set()
        action_errors: dict[str, str] = {}
        try:
            await self.cd2.cancel_uploads(keys)
            succeeded.update(keys)
        except CD2Error as exc:
            logger.error("批量取消上传失败: %s", exc)
            for key in keys:
                action_errors[key] = str(exc)

        # ---- 写重试队列 ----
        for match, task in targets:
            snapshot = snapshots.get(task.key)
            if snapshot is None:
                continue

            ok = task.key in succeeded
            error_text = action_errors.get(task.key, "")
            if not ok:
                # 拦截失败：保持 active，下一轮再试，不写队列以免状态错乱
                log_event(
                    "error",
                    "scheduler",
                    f"拦截失败: {task.file_name}",
                    detail=f"action=cancel err={error_text}",
                )
                continue

            snapshot.state = "intercepted"
            elapsed = max(0.0, now - snapshot.first_seen_at)

            # 已有记录则重置为新一轮重试
            row = (
                session.query(NonInstantTask)
                .filter(NonInstantTask.task_key == task.key)
                .order_by(NonInstantTask.id.desc())
                .first()
            )
            if row is None:
                row = NonInstantTask(
                    task_key=task.key,
                    file_name=task.file_name,
                    source_path=task.source_path,
                    dest_path=task.dest_path,
                    size=task.size,
                    rule_id=match.rule_id,
                    rule_name=match.rule_name,
                    first_seen_at=snapshot.first_seen_at,
                    max_retry=match.max_retry,
                    retry_interval=match.retry_interval,
                    retry_strategy=match.retry_strategy,
                    intercept_action=match.intercept_action,
                    timeout_threshold=match.timeout_threshold,
                )
                session.add(row)
            else:
                row.file_name = task.file_name
                row.source_path = task.source_path
                row.dest_path = task.dest_path
                row.size = task.size
                row.rule_id = match.rule_id
                row.rule_name = match.rule_name
                row.max_retry = match.max_retry
                row.retry_interval = match.retry_interval
                row.retry_strategy = match.retry_strategy
                row.intercept_action = match.intercept_action
                row.timeout_threshold = match.timeout_threshold
                row.first_seen_at = snapshot.first_seen_at

            row.retry_count = 0
            row.intercepted_at = now
            row.last_uploaded_sec = round(elapsed, 3)
            row.attempt_seen_at = None
            row.finished_at = None
            row.error_message = error_text
            row.status = C.STATUS_WAITING
            row.next_retry_at = now + match.retry_interval

            log_event(
                "info",
                "scheduler",
                f"判定不能秒传并拦截: {task.file_name}",
                detail=(
                    f"action={match.intercept_action} "
                    f"elapsed={elapsed:.1f}s > threshold={match.timeout_threshold}s "
                    f"rule={match.rule_name} next_retry_in={match.retry_interval}s"
                ),
                session=session,
            )
            logger.info(
                "拦截[%s]: %s（已上传 %.1fs > 阈值 %ds），%ds 后重试",
                match.intercept_action, task.file_name, elapsed,
                match.timeout_threshold, match.retry_interval,
            )

    # ======================================================================
    # 重试队列
    # ======================================================================
    async def process_retry_queue(self) -> None:
        """到点的重试任务执行 pause_resume / cancel_reupload。"""
        now = utcnow_ts()
        self.stats["retry_last_run"] = now

        with session_scope() as session:
            due_rows = (
                session.query(NonInstantTask)
                .filter(NonInstantTask.status == C.STATUS_WAITING)
                .filter(NonInstantTask.next_retry_at.isnot(None))
                .filter(NonInstantTask.next_retry_at <= now)
                .order_by(NonInstantTask.next_retry_at.asc())
                .limit(C.MAX_RETRY_PER_ROUND)
                .all()
            )
            if not due_rows:
                return

            # 上传来自 CD2 备份任务，重试方式统一为：
            # 让备份重新扫描源目录（BackupRestartWalkingThrough），
            # 备份会重新比对目标端，缺失的文件就会重新上传。
            for row in due_rows:
                if row.retry_count >= row.max_retry:
                    row.status = C.STATUS_FAILED
                    row.finished_at = now
                    row.next_retry_at = None
                    row.error_message = "已达到最大重试次数"
                    log_event(
                        "warning",
                        "scheduler",
                        f"最终失败: {row.file_name}",
                        detail=f"retry={row.retry_count}/{row.max_retry}",
                    )
                    continue

                try:
                    ok = await self._retrigger(
                        row.file_name, row.source_path, row.dest_path
                    )
                except CD2Error as exc:
                    ok = False
                    row.error_message = str(exc)

                row.retry_count += 1
                row.last_retry_at = now
                if ok:
                    row.status = C.STATUS_RETRYING
                    row.attempt_seen_at = now
                    row.next_retry_at = None
                    logger.info(
                        "已触发备份重扫: %s（第 %d/%d 次重试）",
                        row.file_name, row.retry_count, row.max_retry,
                    )
                else:
                    if row.retry_count >= row.max_retry:
                        row.status = C.STATUS_FAILED
                        row.finished_at = now
                        row.next_retry_at = None
                        row.error_message = "无法重新触发上传且已达最大次数"
                        log_event(
                            "warning",
                            "scheduler",
                            f"最终失败（无法重新触发）: {row.file_name}",
                            detail=row.source_path,
                        )
                    else:
                        row.status = C.STATUS_WAITING
                        row.next_retry_at = now + row.retry_interval

            session.flush()

        # ---- 超时兜底：重试后长时间无进展则判失败 ----
        await self._expire_stuck_retries()

    async def _expire_stuck_retries(self) -> None:
        """重试中但长时间没有完成的任务，按最大次数判定。"""
        now = utcnow_ts()
        with session_scope() as session:
            rows = (
                session.query(NonInstantTask)
                .filter(NonInstantTask.status == C.STATUS_RETRYING)
                .filter(NonInstantTask.attempt_seen_at.isnot(None))
                .all()
            )
            for row in rows:
                started = row.attempt_seen_at or now
                # 给本轮一个宽松上限：阈值 × 2 + 60s
                limit = row.timeout_threshold * 2 + 60
                if now - started <= limit:
                    continue

                if row.retry_count >= row.max_retry:
                    row.status = C.STATUS_FAILED
                    row.finished_at = now
                    row.next_retry_at = None
                    row.error_message = "重试未完成且已达最大次数"
                    log_event(
                        "warning",
                        "scheduler",
                        f"最终失败: {row.file_name}",
                        detail=(
                            f"retry={row.retry_count}/{row.max_retry} "
                            f"elapsed={now - started:.0f}s"
                        ),
                    )
                else:
                    row.status = C.STATUS_WAITING
                    row.next_retry_at = now + row.retry_interval
                    row.attempt_seen_at = None
                    logger.info(
                        "重试未完成，排队下一轮: %s（%d/%d）",
                        row.file_name, row.retry_count, row.max_retry,
                    )

    # ======================================================================
    # 115 云下载清理
    # ======================================================================
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
            tasks = await self.p115.list_offline_tasks(page_size=page_size)
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
