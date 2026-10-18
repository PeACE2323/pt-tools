"""作用域过滤测试：不在规则范围内的上传任务不应被记录。

对应需求：“如果不在规则监控范围内的上传，可以不用记录，比如我只监控源
文件夹带有关键字 'PT' 的上传任务，那么其他源文件夹的上传任务不用理会。”
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.rules import match_rule  # noqa: E402


class FakeRule:
    def __init__(self, id: int, name: str, keywords: str, **kw) -> None:
        self.id = id
        self.name = name
        self.source_keywords = keywords
        self.enabled = kw.get("enabled", True)
        self.is_regex = kw.get("is_regex", False)
        self.timeout_threshold = kw.get("timeout", 60)
        self.intercept_action = kw.get("action", "pause")
        self.retry_strategy = kw.get("strategy", "pause_resume")
        self.max_retry = kw.get("max_retry", 3)
        self.retry_interval = kw.get("interval", 300)
        self.operator_filter = kw.get("operator", "any")

    def keyword_list(self) -> list[str]:
        return [x.strip() for x in self.source_keywords.splitlines() if x.strip()]


class FakeTask:
    def __init__(self, key: str, dest: str, override: str = "") -> None:
        self.key = key
        self.dest_path = dest
        self.source_override = override
        self.operator_type = 0

    @property
    def source_path(self) -> str:
        if self.source_override:
            return self.source_override
        if "/" in self.key or "\\" in self.key:
            return self.key
        return self.dest_path


def test_pt_scope_matches_only_pt():
    """只监控含 'PT' 的任务：PT 任务命中，其他不命中。"""
    rule = FakeRule(1, "PT 任务", "PT")

    pt_task = FakeTask("k1", "/115/CloudDrive/PT/movies/a.mkv")
    other_task = FakeTask("k2", "/115/CloudDrive/Docker/b.tar")

    assert match_rule(pt_task, [rule]) is not None, "PT 任务应命中"
    assert match_rule(other_task, [rule]) is None, "非 PT 任务不应命中"
    print("✓ 仅 PT 关键字命中对应任务")


def test_pt_with_webhook_source_override():
    """webhook 补全真实源路径后，按源路径匹配（而非 destPath）。"""
    rule = FakeRule(1, "PT 任务", "/mnt/local/PT")

    # destPath 里不含 PT，但真实源路径含 PT
    task = FakeTask(
        "k1",
        "/115/CloudDrive/movies/a.mkv",
        override="/mnt/local/PT/movies/a.mkv",
    )
    assert match_rule(task, [rule]) is not None, "应能按 webhook 源路径命中"
    print("✓ webhook 源路径参与匹配")


def test_no_rules_means_no_match():
    """没有规则时任何任务都不命中 → 全部不记录。"""
    assert match_rule(FakeTask("k", "/115/PT/a.mkv"), []) is None
    print("✓ 无规则时全部不命中")


def test_disabled_scope_rule():
    """规则禁用后该范围内的任务也不再被记录。"""
    rule = FakeRule(1, "PT", "PT", enabled=False)
    assert match_rule(FakeTask("k", "/115/PT/a.mkv"), [rule]) is None
    print("✓ 禁用规则不再纳入监控范围")


def test_empty_keywords_never_match():
    """关键字为空的规则不应匹配一切。"""
    rule = FakeRule(1, "空规则", "")
    assert match_rule(FakeTask("k", "/115/PT/a.mkv"), [rule]) is None
    print("✓ 空关键字规则不匹配任何任务")


def test_case_insensitive_pt():
    """'PT' 大小写不敏感，能匹配 'pt' / 'Pt'。"""
    rule = FakeRule(1, "PT", "PT")
    assert match_rule(FakeTask("k", "/115/pt/a.mkv"), [rule]) is not None
    assert match_rule(FakeTask("k", "/115/Pt/a.mkv"), [rule]) is not None
    print("✓ PT 匹配大小写不敏感")


def test_partial_word_match_documented():
    """注意：子串匹配意味着 'PT' 也会命中 'PTX' / 'apt' 等。

    这是子串语义的固有行为。若需要精确匹配目录名，用户应改用正则，
    例如 ``/PT/`` 或 ``^/mnt/local/PT/``。
    """
    rule = FakeRule(1, "PT", "PT")
    assert match_rule(FakeTask("k", "/115/apt/a.mkv"), [rule]) is not None
    print("✓ 记录子串匹配的边界行为（PT 也匹配 apt）")


def test_regex_precise_directory():
    """用正则做精确目录匹配，避免误伤。"""
    rule = FakeRule(1, "PT 目录", r"/PT/", is_regex=True)
    assert match_rule(FakeTask("k", "/mnt/local/PT/a.mkv"), [rule]) is not None
    assert match_rule(FakeTask("k", "/mnt/local/apt/a.mkv"), [rule]) is None
    print("✓ 正则 /PT/ 可精确限定目录")


def test_multiple_scope_rules():
    """多条作用域规则：命中任一条即纳入监控。"""
    r_pt = FakeRule(1, "PT", "PT")
    r_movie = FakeRule(2, "电影", "电影")

    assert match_rule(FakeTask("k", "/115/PT/a.mkv"), [r_pt, r_movie]) is not None
    assert match_rule(FakeTask("k", "/115/电影/b.mkv"), [r_pt, r_movie]) is not None
    assert match_rule(FakeTask("k", "/115/其他/c.mkv"), [r_pt, r_movie]) is None
    print("✓ 多规则作用域为或关系")


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
