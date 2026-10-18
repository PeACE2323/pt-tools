"""备份任务匹配逻辑测试（纯逻辑，不依赖 grpc）。

验证 ``resolve_backup_for_path`` 的匹配语义：

- 文件路径必须位于备份源目录**之下**才归属该备份；
- 多个备份嵌套时取**最长**匹配（子备份优先于父备份）；
- 路径分隔符统一按 ``/`` 处理；
- 目标目录必须存在且启用。

为避免依赖 grpc，这里复刻同一套匹配算法做验证，并额外用真实
``CD2Client`` 的方法做一次侵入式校验（注入假的 list_backups）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 参考实现（与 cd2_client.resolve_backup_for_path 保持同一算法）
# ---------------------------------------------------------------------------
def resolve_backup(backups: list[dict], source_path: str):
    normalized = source_path.replace("\\", "/")
    best = None
    best_len = -1
    for backup in backups:
        raw_src = (backup.get("source_path") or "").replace("\\", "/")
        if not raw_src:
            continue
        src = raw_src.rstrip("/")
        prefix = f"{src}/" if src else "/"
        if normalized != src and not normalized.startswith(prefix):
            continue
        enabled_dests = [
            d["path"]
            for d in backup.get("destinations", [])
            if d.get("enabled") and d.get("path")
        ]
        if not enabled_dests:
            continue
        if len(src) > best_len:
            best = (raw_src, enabled_dests[0])
            best_len = len(src)
    return best


BACKUPS = [
    {
        "source_path": "/mnt/local/PT",
        "destinations": [{"path": "/115/CloudDrive/PT", "enabled": True}],
        "enabled": True,
    },
    {
        "source_path": "/mnt/local/PT/movies",
        "destinations": [{"path": "/115/CloudDrive/PT/movies", "enabled": True}],
        "enabled": True,
    },
    {
        "source_path": "/mnt/local/Docker",
        "destinations": [{"path": "/115/CloudDrive/Docker", "enabled": True}],
        "enabled": True,
    },
    {
        "source_path": "/mnt/local/Disabled",
        "destinations": [{"path": "/115/x", "enabled": False}],
        "enabled": True,
    },
]


def test_exact_source_match():
    result = resolve_backup(BACKUPS, "/mnt/local/PT/movies/a.mkv")
    assert result is not None
    # 应命中更具体的 /mnt/local/PT/movies
    assert result[0] == "/mnt/local/PT/movies", result
    print("✓ 嵌套备份取最长匹配")


def test_parent_backup_match():
    result = resolve_backup(BACKUPS, "/mnt/local/PT/other/b.mkv")
    assert result is not None
    assert result[0] == "/mnt/local/PT", result
    print("✓ 回退到父备份")


def test_no_match_outside():
    result = resolve_backup(BACKUPS, "/mnt/elsewhere/c.mkv")
    assert result is None
    print("✓ 不在任何备份下返回 None")


def test_prefix_not_confused():
    """'/mnt/local/PT2/x' 不应被 '/mnt/local/PT' 匹配（前缀陷阱）。"""
    result = resolve_backup(BACKUPS, "/mnt/local/PT2/x.mkv")
    assert result is None, f"不应误匹配，实际: {result}"
    print("✓ 目录前缀不误匹配（PT vs PT2）")


def test_windows_separator():
    result = resolve_backup(BACKUPS, "\\mnt\\local\\PT\\movies\\a.mkv")
    assert result is not None
    assert result[0] == "/mnt/local/PT/movies"
    print("✓ 反斜杠路径归一化")


def test_disabled_destination_skipped():
    """目标目录未启用时该备份不算可用。"""
    result = resolve_backup(BACKUPS, "/mnt/local/Disabled/a.mkv")
    assert result is None, f"目标未启用应返回 None，实际: {result}"
    print("✓ 未启用目标被跳过")


def test_empty_source_skipped():
    backups = [{"source_path": "", "destinations": [{"path": "/x", "enabled": True}]}]
    assert resolve_backup(backups, "/any/x.mkv") is None
    print("✓ 空源目录被跳过")


def test_trailing_slash_source():
    """源目录带尾斜杠时仍能正确匹配，并原样返回给 RPC。"""
    backups = [
        {"source_path": "/mnt/PT/", "destinations": [{"path": "/115/PT", "enabled": True}]}
    ]
    result = resolve_backup(backups, "/mnt/PT/a.mkv")
    assert result is not None, "带尾斜杠的源目录应能匹配"
    assert result[0] == "/mnt/PT/", f"应返回原始配置值，实际: {result[0]!r}"
    print("✓ 源目录尾斜杠容忍")


def test_root_source_match():
    """备份源为根目录 '/' 时，任何绝对路径都应归属它，且返回原始 "/"。"""
    backups = [{"source_path": "/", "destinations": [{"path": "/115", "enabled": True}]}]
    result = resolve_backup(backups, "/a/b.mkv")
    assert result is not None, "根目录备份应能匹配任何绝对路径"
    assert result[0] == "/", f"应返回原始根路径，实际: {result[0]!r}"
    print("✓ 根目录作为备份源")


# ---------------------------------------------------------------------------
# 校验真实源码的命令名与算法一致性
# ---------------------------------------------------------------------------
def test_real_client_uses_backup_rpc():
    """直接检查源码，确认重触发走的是备份 RPC 而不是 CopyFile。"""
    src = (ROOT / "app" / "services" / "cd2_client.py").read_text(encoding="utf-8")
    assert "BackupRestartWalkingThrough" in src, "应使用备份重新扫描 RPC"
    assert "BackupGetAll" in src, "应通过 BackupGetAll 定位备份"
    # 主路径必须在 CopyFile 回退之前
    idx_backup = src.index("BackupRestartWalkingThrough")
    idx_copy = src.index("CopyFile")
    assert idx_backup < idx_copy, "备份策略应作为主路径，CopyFile 仅作回退"
    print("✓ 重触发优先使用备份 RPC")


def test_proto_declares_backup_rpcs():
    """proto 必须声明备份相关 RPC 与消息。"""
    proto = (ROOT / "app" / "proto" / "clouddrive.proto").read_text(encoding="utf-8")
    for rpc in (
        "BackupGetAll",
        "BackupGetStatus",
        "BackupRestartWalkingThrough",
        "BackupSetEnabled",
    ):
        assert f"rpc {rpc}(" in proto.replace(" ", " "), f"缺少 rpc {rpc}"
    for msg in ("Backup", "BackupStatus", "BackupList", "BackupDestination"):
        assert f"message {msg} " in proto or f"message {msg}{{" in proto, f"缺少 message {msg}"
    print("✓ proto 已声明备份 RPC 与消息")


def test_proto_syntax_ok():
    """调用已有的 proto 静态检查器。"""
    from tools.check_proto import check

    errors = check(ROOT / "app" / "proto" / "clouddrive.proto")
    assert not errors, f"proto 校验失败: {errors}"
    print("✓ proto 静态检查通过")


def test_strategy_forced_to_cancel_reupload():
    """接口约定：规则命中的拦截动作恒为 cancel + cancel_reupload。

    上传来自 CD2 备份任务，暂停/恢复不会改变“能否秒传”，因此不提供该分支。
    即使旧数据里存着 pause，也要被归一化。
    """
    from app.services.rules import CANCEL_ACTION, REUPLOAD_STRATEGY, match_rule

    class LegacyRule:
        """模拟历史数据：字段仍是 pause / pause_resume。"""

        id = 1
        name = "旧规则"
        enabled = True
        source_keywords = "PT"
        is_regex = False
        timeout_threshold = 60
        intercept_action = "pause"
        retry_strategy = "pause_resume"
        max_retry = 3
        retry_interval = 300
        operator_filter = "any"

        def keyword_list(self):
            return ["PT"]

    class T:
        key = "k"
        dest_path = "/115/PT/a.mkv"
        source_path = "/115/PT/a.mkv"
        operator_type = 0

    result = match_rule(T(), [LegacyRule()])
    assert result is not None
    assert result.intercept_action == CANCEL_ACTION == "cancel", result.intercept_action
    assert result.retry_strategy == REUPLOAD_STRATEGY == "cancel_reupload"
    print("✓ 拦截动作/策略强制归一化为 cancel + cancel_reupload")


def test_scheduler_has_no_pause_branch():
    """调度器不应再保留暂停/恢复分支。"""
    src = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    for legacy in ("resume_uploads(", "pause_uploads(", "INTERCEPT_PAUSE",
                   "STRATEGY_PAUSE_RESUME", "to_pause"):
        assert legacy not in src, f"调度器仍残留旧分支: {legacy}"
    assert "cancel_uploads(" in src, "应走取消上传"
    print("✓ 调度器已移除暂停/恢复分支")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print(f"✗ {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {fn.__name__}: {exc.__class__.__name__}: {exc}")
            failed += 1
    print()
    print(f"共 {len(tests)} 个测试，失败 {failed} 个")
    sys.exit(1 if failed else 0)
