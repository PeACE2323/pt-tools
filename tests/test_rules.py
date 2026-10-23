"""规则匹配与状态机单元测试。

刻意只依赖 services/rules.py 与 services/p115_client.py 中的纯逻辑部分，
不需要 SQLAlchemy / APScheduler / grpc / p115client 也能跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.rules import (  # noqa: E402
    MatchResult,
    build_matcher as _build_matcher,
    match_rule,
)


class FakeTask:
    """模拟 UploadTask（只暴露匹配所需的属性）。"""

    def __init__(self, key: str, dest: str, operator: int = 0) -> None:
        self.key = key
        self.dest_path = dest
        self.source_path = key if ("/" in key or "\\" in key) else dest
        self.operator_type = operator


def make_task(key: str, dest: str, operator: int = 0):
    return FakeTask(key, dest, operator)


class FakeRule:
    """模拟 Rule 模型（只暴露 match_rule 用到的属性）。"""

    def __init__(
        self,
        id: int,
        name: str,
        keywords: str,
        enabled: bool = True,
        is_regex: bool = False,
        timeout: int = 60,
        action: str = "pause",
        strategy: str = "pause_resume",
        max_retry: int = 3,
        interval: int = 300,
        priority: int = 100,
        operator: str = "any",
    ) -> None:
        self.id = id
        self.name = name
        self.source_keywords = keywords
        self.enabled = enabled
        self.is_regex = is_regex
        self.timeout_threshold = timeout
        self.intercept_action = action
        self.retry_strategy = strategy
        self.max_retry = max_retry
        self.retry_interval = interval
        self.priority = priority
        self.operator_filter = operator

    def keyword_list(self) -> list[str]:
        return [x.strip() for x in self.source_keywords.splitlines() if x.strip()]


def make_task(key: str, dest: str, operator: int = 0) -> FakeTask:
    return FakeTask(key, dest, operator)


def test_substring_match():
    rule = FakeRule(1, "movies", "/media/movies\n/anime")
    task = make_task("f1", "/115/CloudDrive/media/movies/a.mkv")
    result = match_rule(task, [rule])
    assert result is not None, "应命中 /media/movies"
    assert result.rule_id == 1
    print("✓ 子串匹配")


def test_case_insensitive():
    rule = FakeRule(1, "r", "Movies")
    task = make_task("f1", "/x/movies/y.mkv")
    assert match_rule(task, [rule]) is not None
    print("✓ 子串匹配大小写不敏感")


def test_no_match():
    rule = FakeRule(1, "r", "/media/movies")
    task = make_task("f1", "/other/path/b.mkv")
    assert match_rule(task, [rule]) is None
    print("✓ 未命中返回 None")


def test_regex_match():
    rule = FakeRule(1, "r", r"\.(mkv|mp4)$", is_regex=True)
    task = make_task("f1", "/x/a.mkv")
    assert match_rule(task, [rule]) is not None
    task2 = make_task("f2", "/x/a.txt")
    assert match_rule(task2, [rule]) is None
    print("✓ 正则匹配")


def test_invalid_regex_falls_back():
    """非法正则应降级为普通关键字，而不是抛异常。"""
    rule = FakeRule(1, "r", "[unclosed", is_regex=True)
    matcher = _build_matcher(rule)
    assert matcher is not None
    # 降级为子串匹配，路径中不含 "[unclosed"
    assert matcher("/x/a.mkv") is False
    print("✓ 非法正则降级为普通关键字")


def test_priority_order():
    """按传入顺序（已按 priority 排序）取第一条命中的规则。"""
    r_high = FakeRule(1, "high", "/media", priority=10)
    r_low = FakeRule(2, "low", "/media/movies", priority=100)
    task = make_task("f1", "/media/movies/a.mkv")
    result = match_rule(task, [r_high, r_low])
    assert result is not None and result.rule_id == 1
    print("✓ 优先级顺序生效")


def test_disabled_rule_skipped():
    r1 = FakeRule(1, "off", "/media", enabled=False)
    r2 = FakeRule(2, "on", "/media")
    task = make_task("f1", "/media/a.mkv")
    result = match_rule(task, [r1, r2])
    assert result is not None and result.rule_id == 2
    print("✓ 禁用规则被跳过")


def test_operator_filter():
    rule = FakeRule(1, "r", "/media", operator="copy")
    # operator_type=0 (Mount) 不匹配 copy(1)
    task_mount = make_task("f1", "/media/a.mkv", operator=0)
    assert match_rule(task_mount, [rule]) is None
    # operator_type=1 (Copy) 匹配
    task_copy = make_task("f2", "/media/a.mkv", operator=1)
    assert match_rule(task_copy, [rule]) is not None
    print("✓ 操作类型过滤")


def test_multiple_keywords_or_semantics():
    """多关键字之间是“或”。"""
    rule = FakeRule(1, "r", "/aa\n/bb")
    assert match_rule(make_task("k", "/bb/x"), [rule]) is not None
    assert match_rule(make_task("k", "/aa/x"), [rule]) is not None
    assert match_rule(make_task("k", "/cc/x"), [rule]) is None
    print("✓ 多关键字为或关系")


def test_timeout_threshold_from_rule():
    rule = FakeRule(1, "r", "/media", timeout=120, action="cancel", strategy="cancel_reupload", max_retry=5, interval=600)
    result = match_rule(make_task("k", "/media/a"), [rule])
    assert result is not None
    assert result.timeout_threshold == 120
    assert result.intercept_action == "cancel"
    assert result.retry_strategy == "cancel_reupload"
    assert result.max_retry == 5
    assert result.retry_interval == 600
    print("✓ 规则参数正确传递到 MatchResult")


def test_task_source_path_fallback():
    """UploadTask.source_path 的三级回退逻辑（用真实 dataclass）。"""
    from app.services.cd2_client import UploadTask

    def _t(key: str, dest: str) -> UploadTask:
        return UploadTask(
            key=key,
            dest_path=dest,
            size=100,
            transferred_bytes=10,
            status_text="Transfer",
            status_enum=3,
            operator_type=0,
            error_message="",
        )

    # key 含路径 → 用 key
    t2 = _t("/src/dir/c.mkv", "/dst/c.mkv")
    assert t2.source_path == "/src/dir/c.mkv", "key 含路径时应优先用 key"
    # key 非路径 → 回退 dest_path
    t3 = _t("opaque-key-123", "/dst/c.mkv")
    assert t3.source_path == "/dst/c.mkv", "key 非路径时回退 dest_path"
    # 文件名提取
    assert t3.file_name == "c.mkv"
    assert t3.progress == 10.0
    print("✓ source_path 回退逻辑")


def test_offline_task_status():
    """115 云下载状态归一化。"""
    from app.services.p115_client import OfflineTask

    # 进度 100 → 完成
    t = OfflineTask("h1", "n", 1, 0, 0, 100.0)
    assert t.normalized_status == "finished"
    assert t.is_terminal
    # 进度 50 → 下载中
    t2 = OfflineTask("h2", "n", 1, 0, 0, 50.0)
    assert t2.normalized_status == "downloading"
    assert not t2.is_terminal
    # 进度 99 → 仍是下载中（99% 未完成，需参与超时判定）
    t99 = OfflineTask("h99", "n", 1, 0, 1, 99.0)
    assert t99.normalized_status == "downloading"
    assert not t99.is_terminal
    # 进度 0 → 下载中（未完成，参与超时判定）
    t3 = OfflineTask("h3", "n", 1, 0, 0, 0.0)
    assert t3.normalized_status == "downloading"
    assert not t3.is_terminal
    # status=9 → 失败
    t5 = OfflineTask("h5", "n", 1, 0, 9, 30.0)
    assert t5.normalized_status == "failed"
    assert t5.is_terminal
    print("✓ 115 任务状态归一化")


def test_offline_age():
    from app.services.p115_client import OfflineTask

    now = 1_000_000.0
    t = OfflineTask("h", "n", 1, int(now - 3600), 0, 50.0)
    assert abs(t.age_seconds(now) - 3600) < 1
    # add_time=0 时不报错
    t2 = OfflineTask("h2", "n", 1, 0, 0, 50.0)
    assert t2.age_seconds(now) == 0.0
    print("✓ 存在时长计算")


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
