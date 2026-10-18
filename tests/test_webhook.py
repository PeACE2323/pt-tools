"""Webhook 解析单元测试。

重点覆盖最容易出错的点：
- ``is_dir`` 在官方模板里是字符串 ``"false"`` / ``"true"``
- ``event_time`` 受 CD2 time_format 控制，可能是 epoch / RFC3339 / 自定义格式
- rename 的 destination_file 语义
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.webhook_parser import (  # noqa: E402
    FileChange,
    parse_payload,
    to_bool as _to_bool,
    to_timestamp as _to_timestamp,
)


def test_is_dir_string_false():
    """官方模板把 is_dir 写成字符串，必须正确解析为 False。"""
    assert _to_bool("false") is False
    assert _to_bool("False") is False
    assert _to_bool("0") is False
    assert _to_bool("") is False
    assert _to_bool(None) is False
    print("✓ is_dir 字符串 'false' 解析为 False")


def test_is_dir_string_true():
    assert _to_bool("true") is True
    assert _to_bool("True") is True
    assert _to_bool("1") is True
    assert _to_bool(True) is True
    print("✓ is_dir 字符串 'true' 解析为 True")


def test_timestamp_epoch():
    assert _to_timestamp(1700000000) == 1700000000.0
    assert _to_timestamp("1700000000") == 1700000000.0
    print("✓ epoch 时间戳解析")


def test_timestamp_millis():
    """毫秒级时间戳应被识别（> 1e11）。"""
    assert abs(_to_timestamp(1700000000000) - 1700000000.0) < 1
    print("✓ 毫秒时间戳解析")


def test_timestamp_rfc3339():
    ts = _to_timestamp("2024-01-15T10:30:00Z")
    assert ts > 0
    # 只用 UTC 解释，不依赖本机时区
    import datetime as dt

    assert dt.datetime.fromtimestamp(ts, dt.timezone.utc).year == 2024
    print("✓ RFC3339 时间解析")


def test_timestamp_invalid():
    assert _to_timestamp("not-a-time") == 0.0
    assert _to_timestamp(None) == 0.0
    assert _to_timestamp("") == 0.0
    print("✓ 非法时间返回 0 不抛异常")


def test_parse_official_payload():
    """模拟 CD2 官方模板发出的完整 body。"""
    body = {
        "device_name": "nas01",
        "user_name": "admin",
        "version": "1.0.17",
        "event_category": "file",
        "event_name": "notify",
        "event_time": "1700000000",
        "send_time": "1700000001",
        "data": [
            {
                "action": "create",
                "is_dir": "false",
                "source_file": "/mnt/local/PT/movies/xxx.mkv",
                "destination_file": "",
            }
        ],
    }
    payload = parse_payload(body)
    assert payload.device_name == "nas01"
    assert payload.event_category == "file"
    assert payload.event_name == "notify"
    assert len(payload.changes) == 1

    change = payload.changes[0]
    assert change.action == "create"
    assert change.is_dir is False
    assert change.is_file
    assert change.source_file == "/mnt/local/PT/movies/xxx.mkv"
    assert change.file_name == "xxx.mkv"
    print("✓ 官方模板 payload 解析")


def test_parse_dir_event_skipped_later():
    """目录事件的 is_dir=true，记录层应跳过（这里只验证解析）。"""
    body = {
        "data": [{"action": "create", "is_dir": "true", "source_file": "/mnt/PT/sub"}]
    }
    payload = parse_payload(body)
    assert len(payload.changes) == 1
    assert payload.changes[0].is_dir is True
    assert payload.changes[0].is_file is False
    print("✓ 目录事件解析")


def test_parse_rename_uses_destination():
    body = {
        "data": [
            {
                "action": "rename",
                "is_dir": "false",
                "source_file": "/mnt/old/a.mkv",
                "destination_file": "/mnt/new/b.mkv",
            }
        ]
    }
    change = parse_payload(body).changes[0]
    assert change.effective_path == "/mnt/new/b.mkv"
    assert change.file_name == "b.mkv"
    print("✓ rename 使用 destination_file")


def test_parse_move_alias():
    """move 是 rename 的别名。"""
    body = {
        "data": [
            {
                "action": "move",
                "is_dir": "false",
                "source_file": "/a.mkv",
                "destination_file": "/b.mkv",
            }
        ]
    }
    change = parse_payload(body).changes[0]
    assert change.action == "rename"
    print("✓ move 归一化为 rename")


def test_parse_unknown_action_ignored():
    body = {"data": [{"action": "chmod", "source_file": "/a.mkv"}]}
    assert parse_payload(body).changes == []
    print("✓ 未知 action 被忽略")


def test_parse_missing_data():
    assert parse_payload({}).changes == []
    assert parse_payload({"data": None}).changes == []
    assert parse_payload({"data": "garbage"}).changes == []
    print("✓ data 缺失/异常时安全返回")


def test_parse_single_dict_data():
    """data 只给单个对象（非数组）也应能解析。"""
    body = {"data": {"action": "create", "is_dir": "false", "source_file": "/x.mkv"}}
    payload = parse_payload(body)
    assert len(payload.changes) == 1
    print("✓ data 为单对象时兼容")


def test_parse_empty_source_skipped():
    body = {"data": [{"action": "create", "is_dir": "false", "source_file": ""}]}
    assert parse_payload(body).changes == []
    print("✓ 空 source_file 被跳过")


def test_file_change_slots_dataclass():
    change = FileChange(action="create", is_dir=False, source_file="/a/b.mkv")
    assert change.file_name == "b.mkv"
    change_dir = FileChange(action="create", is_dir=True, source_file="/a/bdir/")
    assert change_dir.file_name == "bdir"
    print("✓ FileChange 文件名提取（含尾斜杠）")


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
