"""SQLAlchemy 数据模型与数据库初始化。

对应 docs/schema.sql 中的表结构。时间字段统一使用 UTC Unix 时间戳（float），
避免容器时区与宿主机不一致导致的超时误判；仅 created_at/updated_at
这类审计字段使用 datetime。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import DATABASE_URL, DB_PATH, DEFAULT_SETTINGS
from .utils import get_logger

logger = get_logger("guard.models")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ===========================================================================
# 1. 全局配置
# ===========================================================================
class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt, onupdate=_now_dt
    )


# ===========================================================================
# 2. 规则
# ===========================================================================
class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 源文件夹路径关键字：多行文本，每行一个
    source_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 超时判定阈值（秒）
    timeout_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # 拦截动作：pause | cancel
    intercept_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pause"
    )
    # 重试策略：pause_resume | cancel_reupload
    retry_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pause_resume"
    )
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # 重试间隔（秒）
    retry_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # any | mount | copy | backup | remote
    operator_filter: Mapped[str] = mapped_column(
        String(16), nullable=False, default="any"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt, onupdate=_now_dt
    )

    # ------------------------------------------------------------- 便捷属性
    def keyword_list(self) -> list[str]:
        """解析关键字多行文本。"""
        return [
            line.strip()
            for line in (self.source_keywords or "").splitlines()
            if line.strip()
        ]


# ===========================================================================
# 3. 秒传记录
# ===========================================================================
class InstantUpload(Base):
    __tablename__ = "instant_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dest_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    first_seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    finished_at: Mapped[float] = mapped_column(Float, nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # 秒传判定的证据：完成时实际传输的字节数。
    # 真正的秒传该值应接近 0（服务器已有相同哈希，未接收文件数据）。
    # 保留它便于用户自行核对判定是否正确。
    transferred_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )


# ===========================================================================
# 4. 不能秒传 / 重试队列
# ===========================================================================
class NonInstantTask(Base):
    __tablename__ = "non_instant_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dest_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # waiting | retrying | success | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="waiting", index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    retry_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pause_resume"
    )
    intercept_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pause"
    )

    first_seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    intercepted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    last_uploaded_sec: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    next_retry_at: Mapped[float | None] = mapped_column(
        Float, nullable=True, index=True
    )
    last_retry_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 本轮重试（resume 之后）首次观测到任务的时间
    attempt_seen_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt, onupdate=_now_dt
    )


# ===========================================================================
# 5. 实时上传任务快照
# ===========================================================================
class UploadTaskSnapshot(Base):
    __tablename__ = "upload_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dest_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transferred: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 整个生命周期内观测到的**最大**传输字节数。
    #
    # 为什么需要它：``transferred`` 是 CD2 某一瞬间的值，任务完成后
    # CD2 常把它归零或直接移除任务。若只用最后一次快照判断「有没有
    # 传过数据」，会把真实上传误判成秒传（反之也可能漏记）。
    # 判定秒传应该看「这期间最多传到过多少」。
    transferred_peak: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    status_text: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status_enum: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operator_type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    matched_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_rule_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default=""
    )

    first_seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    # queued | active | intercepted | done | gone
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True
    )
    # 累计排队时长（秒）：Preprocessing / Inqueue 阶段的累计时间。
    # CD2 有并发上限，排队不计入「上传超时」，但需要展示给用户。
    queued_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt, onupdate=_now_dt
    )


# ===========================================================================
# 6. 115 云下载任务快照
# ===========================================================================
class P115OfflineTask(Base):
    __tablename__ = "p115_offline_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    add_time: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    percent_done: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    normalized_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="waiting", index=True
    )

    first_seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_seen_at: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt, onupdate=_now_dt
    )


# ===========================================================================
# 7. 115 取消日志
# ===========================================================================
class P115CancelLog(Base):
    __tablename__ = "p115_cancel_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    add_time: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cancel_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    percent_done: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    age_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    threshold_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # auto | manual
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )


# ===========================================================================
# 8. 事件日志
# ===========================================================================
class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt, index=True
    )


# ===========================================================================
# 9. 源路径映射（由 CD2 file_system_watcher webhook 写入）
# ===========================================================================
class SourcePath(Base):
    """记录“文件名 → 真实源路径”的映射。

    CD2 的 gRPC 上传任务不含源路径字段，也缺少“重新上传本地文件”的 RPC。
    webhook 能在文件变化时推送 ``source_file``，这里按 basename 建立映射，
    供两处使用：

    1. 规则匹配：用真实源路径（而非 destPath）匹配关键字；
    2. cancel_reupload 策略：取消任务后据此重新触发上传。
    """

    __tablename__ = "source_paths"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 文件名（basename），用于与 CD2 destPath 的文件名对齐
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    dest_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # webhook action：create / modify / delete / rename
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="create")
    device_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 是否为目录（webhook 里 is_dir 是字符串 "true"/"false"）
    is_dir: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    seen_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now_dt
    )


# ===========================================================================
# 引擎 / 会话
# ===========================================================================
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    # SQLite 跨线程使用：调度器在独立线程/协程里可能短占会话
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """WAL 模式 + 合理超时，避免高频轮询写日志时锁库。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文：异常自动回滚。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用的会话生成器。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ===========================================================================
# 初始化
# ===========================================================================
def init_db() -> None:
    """建表 + 补齐新增列 + 播种默认配置。"""
    Base.metadata.create_all(engine)
    migrate_added_columns()
    logger.info("数据库已就绪: %s", DB_PATH)
    seed_default_settings()


def migrate_added_columns() -> None:
    """给已存在的表补上后来新增的列。

    ``create_all`` 只会建缺失的**表**，不会给已有表加**列**。
    用户升级镜像时数据库还在，缺列会导致 SQL 报错，所以这里做最小迁移。

    只做「加列」，不做删改，安全且幂等。
    """
    from sqlalchemy import inspect, text

    # 表名 -> [(列名, 列定义)]
    wanted: dict[str, list[tuple[str, str]]] = {
        "upload_tasks": [
            ("queued_sec", "FLOAT NOT NULL DEFAULT 0.0"),
            # 判定秒传要看「这期间最多传到过多少」，而不是最后一瞬的值
            ("transferred_peak", "BIGINT NOT NULL DEFAULT 0"),
        ],
        "instant_uploads": [
            ("transferred_bytes", "BIGINT NOT NULL DEFAULT 0"),
        ],
    }

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, columns in wanted.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in have:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    logger.info("已为表 %s 添加列 %s", table, name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("为表 %s 添加列 %s 失败: %s", table, name, exc)


def seed_default_settings() -> None:
    """把缺失的默认配置项补齐（已存在的值不覆盖）。"""
    with session_scope() as session:
        existing = {
            row[0] for row in session.execute(select(AppSetting.key)).all()
        }
        added = 0
        for key, value in DEFAULT_SETTINGS.items():
            if key in existing:
                continue
            session.add(
                AppSetting(key=key, value=value, updated_at=_now_dt())
            )
            added += 1
        if added:
            logger.info("已写入 %d 条默认配置", added)


# ===========================================================================
# 配置读写辅助
# ===========================================================================
def get_settings_map(session: Session | None = None) -> dict[str, str]:
    """读取全部配置为 dict（缺失项用默认值补齐）。"""
    result = dict(DEFAULT_SETTINGS)

    def _read(s: Session) -> None:
        for row in s.execute(select(AppSetting)).scalars():
            result[row.key] = row.value

    if session is not None:
        _read(session)
    else:
        with session_scope() as s:
            _read(s)
    return result


def set_setting(session: Session, key: str, value: Any) -> None:
    """写入单条配置（不存在则创建）。"""
    text = "" if value is None else str(value)
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=text, updated_at=_now_dt()))
    else:
        row.value = text
        row.updated_at = _now_dt()


def get_setting_float(settings: dict[str, str], key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def get_setting_int(settings: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


def get_setting_bool(settings: dict[str, str], key: str, default: bool) -> bool:
    raw = str(settings.get(key, "1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def trim_event_logs(session: Session, keep: int = 5000) -> int:
    """裁剪事件日志，防止无限增长。返回删除条数。"""
    from sqlalchemy import delete, func

    total = session.execute(select(func.count(EventLog.id))).scalar() or 0
    if total <= keep:
        return 0
    # 找到截断点 id
    cutoff_row = session.execute(
        select(EventLog.id).order_by(EventLog.id.desc()).offset(keep).limit(1)
    ).first()
    if not cutoff_row:
        return 0
    cutoff_id = cutoff_row[0]
    result = session.execute(delete(EventLog).where(EventLog.id <= cutoff_id))
    return int(result.rowcount or 0)
