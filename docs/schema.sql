-- ===========================================================================
-- CD2-115-Guard  SQLite 表结构（权威 DDL 参考）
-- ---------------------------------------------------------------------------
-- 说明：
--   1. 运行时表结构由 app/models.py 中的 SQLAlchemy 模型自动建表
--      （Base.metadata.create_all），本文件是等价的手工 DDL 参考，
--      便于审计、备份、迁移与用 sqlite3 直接排查数据。
--   2. 所有时间字段统一存 UTC 的 Unix 时间戳（float，秒，带小数），
--      前端按浏览器本地时区渲染，避免容器 TZ 与宿主机不一致导致错判超时。
--   3. 数据库文件位置：/app/data/guard.db （Docker 挂载卷）
-- ===========================================================================

PRAGMA journal_mode = WAL;      -- 高频轮询写日志时避免读写互锁
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. app_settings —— 全局配置（单行 key/value，JSON 值）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key         VARCHAR(64)  NOT NULL PRIMARY KEY,
    value       TEXT         NOT NULL DEFAULT '',
    updated_at  DATETIME     NOT NULL
);

-- 预置默认配置项（实际由 models.py 的 seed_defaults() 写入完整默认值）
-- key 一览：
--   cd2.host              CD2 gRPC 地址，形如 "clouddrive2:19799" 或 "127.0.0.1:19799"
--   cd2.token             CD2 Bearer Token（也可用账号密码换取的 JWT）
--   cd2.username          CD2 账号（可选，用于 GetToken）
--   cd2.password          CD2 密码（可选，仅存本地 SQLite）
--   cd2.poll_interval     上传任务轮询间隔（秒），默认 3
--   cd2.intercept_action  拦截动作：pause | cancel
--   p115.cookie           115 Cookie 字符串（同时落盘 /app/data/115-cookies.txt）
--   p115.poll_interval    云下载轮询间隔（秒），默认 300
--   p115.timeout_threshold 云下载最大允许耗时（秒），默认 86400
--   p115.auto_cancel      是否自动取消超时云下载任务（0/1）
--   p115.task_page_size   云下载列表分页大小，默认 30

-- ---------------------------------------------------------------------------
-- 2. rules —— CD2 上传任务自定义匹配规则
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    id                     INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    name                   VARCHAR(128) NOT NULL,
    enabled                BOOLEAN  NOT NULL DEFAULT 1,
    -- 源文件夹路径关键字，多行文本，每行一个
    source_keywords        TEXT     NOT NULL DEFAULT '',
    -- 是否把关键字当作正则表达式解释
    is_regex               BOOLEAN  NOT NULL DEFAULT 0,
    -- 超时判定阈值（秒）：上传持续时长超过该值即判定为“不能秒传”
    timeout_threshold      INTEGER  NOT NULL DEFAULT 60,
    -- 拦截动作：pause（暂停，可原地恢复）| cancel（取消，需重新触发上传）
    intercept_action       VARCHAR(16) NOT NULL DEFAULT 'pause',
    -- 重试策略：pause_resume（暂停→等待→恢复）| cancel_reupload（取消→重新触发）
    retry_strategy         VARCHAR(32) NOT NULL DEFAULT 'pause_resume',
    max_retry              INTEGER  NOT NULL DEFAULT 3,
    -- 重试间隔（秒）
    retry_interval         INTEGER  NOT NULL DEFAULT 300,
    -- 命中优先级，数值越小越先匹配
    priority               INTEGER  NOT NULL DEFAULT 100,
    -- 仅匹配指定操作类型：any | mount | copy | backup | remote
    operator_filter        VARCHAR(16) NOT NULL DEFAULT 'any',
    created_at             DATETIME NOT NULL,
    updated_at             DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rules_enabled_priority ON rules (enabled, priority);

-- ---------------------------------------------------------------------------
-- 3. instant_uploads —— 秒传任务详情记录
--    “命中规则且在上传耗时 ≤ timeout_threshold 内完成”的任务
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instant_uploads (
    id             INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    task_key       VARCHAR(255) NOT NULL,            -- CD2 任务 key
    file_name      VARCHAR(512) NOT NULL DEFAULT '',
    source_path    TEXT     NOT NULL DEFAULT '',
    dest_path      TEXT     NOT NULL DEFAULT '',     -- CD2 destPath
    size           BIGINT   NOT NULL DEFAULT 0,
    rule_id        INTEGER,
    rule_name      VARCHAR(128) NOT NULL DEFAULT '',
    first_seen_at  FLOAT    NOT NULL,                -- 首次观测到任务的时间戳
    finished_at    FLOAT    NOT NULL,                -- 判定完成的时间戳
    duration_sec   FLOAT    NOT NULL DEFAULT 0,      -- finished_at - first_seen_at
    created_at     DATETIME NOT NULL
);
-- 同一 task_key 只记一次秒传，避免高频轮询重复插入
CREATE UNIQUE INDEX IF NOT EXISTS ux_instant_task_key ON instant_uploads (task_key);
CREATE INDEX IF NOT EXISTS ix_instant_finished_at ON instant_uploads (finished_at);

-- ---------------------------------------------------------------------------
-- 4. non_instant_tasks —— 不能秒传任务 / 重试队列 / 最终失败
--    状态机： waiting(等待重试) -> retrying(重试中) -> success(重试成功)
--                                                    \-> failed(最终失败)
--            以及 cancelled(用户取消重试)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS non_instant_tasks (
    id                INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    task_key          VARCHAR(255) NOT NULL,
    file_name         VARCHAR(512) NOT NULL DEFAULT '',
    source_path       TEXT     NOT NULL DEFAULT '',
    dest_path         TEXT     NOT NULL DEFAULT '',
    size              BIGINT   NOT NULL DEFAULT 0,
    rule_id           INTEGER,
    rule_name         VARCHAR(128) NOT NULL DEFAULT '',
    -- pending | waiting | retrying | success | failed | cancelled
    status            VARCHAR(16) NOT NULL DEFAULT 'waiting',
    retry_count       INTEGER  NOT NULL DEFAULT 0,
    max_retry         INTEGER  NOT NULL DEFAULT 3,
    retry_interval    INTEGER  NOT NULL DEFAULT 300,
    retry_strategy    VARCHAR(32) NOT NULL DEFAULT 'pause_resume',
    intercept_action  VARCHAR(16) NOT NULL DEFAULT 'pause',
    first_seen_at     FLOAT    NOT NULL,
    intercepted_at    FLOAT,                          -- 触发拦截的时间戳
    timeout_threshold INTEGER  NOT NULL DEFAULT 60,
    last_uploaded_sec FLOAT    NOT NULL DEFAULT 0,    -- 拦截时已上传耗时
    next_retry_at     FLOAT,                          -- 下次重试的时间戳
    last_retry_at     FLOAT,                          -- 最近一次重试的时间戳
    finished_at       FLOAT,                          -- 重试成功/最终失败时间
    attempt_seen_at   FLOAT,                          -- 本轮回重试后首次观测时间
    error_message     TEXT     NOT NULL DEFAULT '',
    created_at        DATETIME NOT NULL,
    updated_at        DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_non_instant_status ON non_instant_tasks (status);
CREATE INDEX IF NOT EXISTS ix_non_instant_next_retry ON non_instant_tasks (next_retry_at);
CREATE INDEX IF NOT EXISTS ix_non_instant_task_key ON non_instant_tasks (task_key);

-- ---------------------------------------------------------------------------
-- 5. upload_tasks —— 实时上传任务快照（first_seen_at 的持久化载体）
--    高频轮询时 upsert；进程重启后仍能算出正确的“已持续上传时间”
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upload_tasks (
    id             INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    task_key       VARCHAR(255) NOT NULL,
    file_name      VARCHAR(512) NOT NULL DEFAULT '',
    source_path    TEXT     NOT NULL DEFAULT '',
    dest_path      TEXT     NOT NULL DEFAULT '',
    size           BIGINT   NOT NULL DEFAULT 0,
    transferred    BIGINT   NOT NULL DEFAULT 0,
    status_text    VARCHAR(64) NOT NULL DEFAULT '',
    status_enum    INTEGER  NOT NULL DEFAULT 0,
    operator_type  INTEGER  NOT NULL DEFAULT 0,
    matched_rule_id INTEGER,
    matched_rule_name VARCHAR(128) NOT NULL DEFAULT '',
    first_seen_at  FLOAT    NOT NULL,
    last_seen_at   FLOAT    NOT NULL,
    -- active | done | intercepted | gone
    state          VARCHAR(16) NOT NULL DEFAULT 'active',
    error_message  TEXT     NOT NULL DEFAULT '',
    created_at     DATETIME NOT NULL,
    updated_at     DATETIME NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_upload_task_key ON upload_tasks (task_key);
CREATE INDEX IF NOT EXISTS ix_upload_state ON upload_tasks (state);

-- ---------------------------------------------------------------------------
-- 6. p115_offline_tasks —— 115 云下载任务快照（驻留时长计算用）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS p115_offline_tasks (
    id            INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    info_hash     VARCHAR(64) NOT NULL,
    name          VARCHAR(512) NOT NULL DEFAULT '',
    size          BIGINT   NOT NULL DEFAULT 0,
    add_time      BIGINT   NOT NULL DEFAULT 0,        -- 115 返回的 add_time（秒）
    status        INTEGER  NOT NULL DEFAULT 0,        -- 115 原始 status
    percent_done  FLOAT    NOT NULL DEFAULT 0,
    -- waiting | downloading | finished | failed | cancelled | removed
    normalized_status VARCHAR(16) NOT NULL DEFAULT 'waiting',
    first_seen_at FLOAT    NOT NULL,                  -- 首次在本系统出现的时间
    last_seen_at  FLOAT    NOT NULL,
    created_at    DATETIME NOT NULL,
    updated_at    DATETIME NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_offline_info_hash ON p115_offline_tasks (info_hash);
CREATE INDEX IF NOT EXISTS ix_offline_status ON p115_offline_tasks (normalized_status);

-- ---------------------------------------------------------------------------
-- 7. p115_cancel_logs —— 115 云下载超时取消日志
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS p115_cancel_logs (
    id            INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    info_hash     VARCHAR(64) NOT NULL,
    name          VARCHAR(512) NOT NULL DEFAULT '',
    add_time      BIGINT   NOT NULL DEFAULT 0,        -- 原添加时间
    cancel_at     FLOAT    NOT NULL,                  -- 取消动作发生时间
    percent_done  FLOAT    NOT NULL DEFAULT 0,        -- 取消时进度
    status        INTEGER  NOT NULL DEFAULT 0,        -- 取消时的 115 原始 status
    age_sec       FLOAT    NOT NULL DEFAULT 0,        -- 取消时已存在时长
    threshold_sec INTEGER  NOT NULL DEFAULT 0,        -- 当时的阈值配置
    success       BOOLEAN  NOT NULL DEFAULT 1,        -- 删除接口是否成功
    message       TEXT     NOT NULL DEFAULT '',
    -- auto | manual
    trigger       VARCHAR(16) NOT NULL DEFAULT 'auto',
    created_at    DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cancel_cancel_at ON p115_cancel_logs (cancel_at);
CREATE INDEX IF NOT EXISTS ix_cancel_info_hash ON p115_cancel_logs (info_hash);

-- ---------------------------------------------------------------------------
-- 8. event_logs —— 系统事件与异常（CD2 断链重连、115 Cookie 失效等）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_logs (
    id         INTEGER  NOT NULL PRIMARY KEY AUTOINCREMENT,
    level      VARCHAR(16) NOT NULL DEFAULT 'info',   -- info | warning | error
    source     VARCHAR(32) NOT NULL DEFAULT 'system', -- cd2 | p115 | scheduler | api
    message    TEXT     NOT NULL DEFAULT '',
    detail     TEXT     NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_created ON event_logs (created_at);
CREATE INDEX IF NOT EXISTS ix_event_level ON event_logs (level);

-- ---------------------------------------------------------------------------
-- 常用排查查询
-- ---------------------------------------------------------------------------
-- 今日秒传数：
--   SELECT date(finished_at, 'unixepoch', 'localtime') d, count(*)
--   FROM instant_uploads GROUP BY d ORDER BY d DESC LIMIT 7;
-- 当前等待重试任务：
--   SELECT id, file_name, retry_count, max_retry, datetime(next_retry_at,'unixepoch','localtime')
--   FROM non_instant_tasks WHERE status IN ('waiting','retrying');
-- 近 24h 被取消的云下载：
--   SELECT name, info_hash, percent_done, datetime(cancel_at,'unixepoch','localtime')
--   FROM p115_cancel_logs WHERE cancel_at > strftime('%s','now') - 86400;
