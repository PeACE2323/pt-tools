"""PT 配置持久化的验证（不依赖 SQLAlchemy）。

用纯 sqlite3 精确复刻 ``save_pt_config`` / ``load_pt_config`` 的 SQL 语义，
验证「保存 → 关闭连接 → 重新打开」后数据仍在。

## 为什么单独做这个验证

用户反馈「重启容器后，PT 策略与下载器设置全部丢失」。

代码层面看读写都正确，所以怀疑点集中在：

1. 数据库文件没落在挂载卷上（`DATA_DIR` 环境变量或卷映射问题）
2. 写入没有真正 commit
3. 启动时被默认值覆盖

本脚本验证第 2、3 点在本仓库的实现下是否成立；第 1 点只能靠运行时诊断。

用法：

    python tests/check_pt_storage.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# 复刻 models.AppSetting + bt_monitor 的读写逻辑
# ===========================================================================
SCHEMA = """
CREATE TABLE app_settings (
    key VARCHAR(64) NOT NULL PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME NOT NULL
);
"""


class FakeSession:
    """模拟 SQLAlchemy Session 的 get / add / commit。"""

    def __init__(self, path: str):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self._pending: list[tuple[str, str]] = []
        self._pending_update: dict[str, str] = {}

    def get(self, key: str):
        if key in self._pending_update:
            return {"key": key, "value": self._pending_update[key]}
        for k, v in self._pending:
            if k == key:
                return {"key": k, "value": v}
        row = self.con.execute(
            "SELECT key, value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def add(self, key: str, value: str) -> None:
        self._pending.append((key, value))

    def set_value(self, key: str, value: str) -> None:
        self._pending_update[key] = value

    def commit(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for key, value in self._pending:
            self.con.execute(
                "INSERT OR REPLACE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, value, now),
            )
        for key, value in self._pending_update.items():
            self.con.execute(
                "UPDATE app_settings SET value=?, updated_at=? WHERE key=?",
                (value, now, key),
            )
        self.con.commit()
        self._pending.clear()
        self._pending_update.clear()

    def close(self) -> None:
        self.con.close()


def save_pt_config(session, clients=None, policies=None) -> None:
    """与 bt_monitor.save_pt_config 同语义。"""
    if clients is not None:
        text = json.dumps(clients, ensure_ascii=False)
        if session.get("pt.clients") is None:
            session.add("pt.clients", text)
        else:
            session.set_value("pt.clients", text)
    if policies is not None:
        text = json.dumps(policies, ensure_ascii=False)
        if session.get("pt.policies") is None:
            session.add("pt.policies", text)
        else:
            session.set_value("pt.policies", text)


def load_pt_config(session) -> tuple[list, list]:
    """与 bt_monitor.load_pt_config 同语义。"""

    def _get(key):
        row = session.get(key)
        if row is None or not row.get("value"):
            return []
        try:
            data = json.loads(row["value"])
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    return _get("pt.clients"), _get("pt.policies")


# ===========================================================================
# 测试
# ===========================================================================
def main() -> int:
    # 写在工作区的临时子目录里（部分沙箱禁止写系统临时目录）
    tmpdir = ROOT / ".tmp_test"
    tmpdir.mkdir(exist_ok=True)
    db = str(tmpdir / "guard.db")
    if Path(db).exists():
        Path(db).unlink()
    failed = 0

    # ---------- 建库 ----------
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    print(f"测试数据库: {db}\n")

    clients = [
        {
            "name": "qb",
            "type": "qbittorrent",
            "url": "http://192.168.7.101:8080",
            "username": "admin",
            "password": "secret",
            "api_key": "qbt_abc",
            "enabled": True,
            "insecure_skip_verify": False,
            "timeout": "10s",
        }
    ]
    policies = [
        {
            "name": "每日 500G",
            "enabled": True,
            "clients": [],
            "window": "24h",
            "align": "day",
            "threshold_total": 536870912000,
            "limit_total": 1048576,
            "resume_limit_total": 0,
            "resume_on_window_reset": True,
            "cooldown": "0s",
        }
    ]

    # ---------- 1. 首次保存 ----------
    print("[1] 首次保存配置")
    s = FakeSession(db)
    save_pt_config(s, clients=clients, policies=policies)
    s.commit()
    s.close()
    print("    ✓ 已写入并 commit")

    # ---------- 2. 模拟容器重启：新连接读取 ----------
    print("[2] 模拟重启后读取")
    s2 = FakeSession(db)
    c2, p2 = load_pt_config(s2)
    s2.close()
    if len(c2) == 1 and len(p2) == 1:
        print(f"    ✓ 下载器 {len(c2)} 个、策略 {len(p2)} 条")
    else:
        print(f"    ✗ 数据丢失：clients={len(c2)}, policies={len(p2)}")
        failed += 1

    # ---------- 3. 再次保存（走 UPDATE 分支） ----------
    print("[3] 二次保存（UPDATE 分支）")
    clients[0]["url"] = "http://192.168.7.200:8080"
    policies[0]["threshold_total"] = 1073741824000
    s3 = FakeSession(db)
    save_pt_config(s3, clients=clients, policies=policies)
    s3.commit()
    s3.close()

    s4 = FakeSession(db)
    c4, p4 = load_pt_config(s4)
    s4.close()
    if c4 and c4[0]["url"] == "http://192.168.7.200:8080":
        print("    ✓ 更新生效")
    else:
        print(f"    ✗ 更新未生效: {c4}")
        failed += 1
    if p4 and p4[0]["threshold_total"] == 1073741824000:
        print("    ✓ 策略更新生效")
    else:
        print(f"    ✗ 策略未更新: {p4}")
        failed += 1

    # ---------- 4. 默认值补齐不应覆盖已有数据 ----------
    print("[4] 启动时补齐默认值不得覆盖")
    DEFAULT = {"cd2.host": "h:19798", "p115.timeout": "3600"}
    s5 = FakeSession(db)
    existing = set()
    for row in s5.con.execute("SELECT key FROM app_settings"):
        existing.add(row["key"])
    for k, v in DEFAULT.items():
        if k not in existing:
            s5.add(k, v)
    s5.commit()
    c5, p5 = load_pt_config(s5)
    s5.close()
    if len(c5) == 1 and len(p5) == 1:
        print("    ✓ 默认值补齐未破坏 PT 配置")
    else:
        print(f"    ✗ 被覆盖：clients={len(c5)}, policies={len(p5)}")
        failed += 1

    # ---------- 5. 密码等敏感字段完整保留 ----------
    print("[5] 敏感字段持久化")
    s6 = FakeSession(db)
    c6, _ = load_pt_config(s6)
    s6.close()
    ok = c6 and c6[0].get("password") == "secret" and c6[0].get("api_key") == "qbt_abc"
    if ok:
        print("    ✓ 密码与 API Key 完整保留")
    else:
        print(f"    ✗ 凭据丢失: {c6}")
        failed += 1

    print()
    if failed:
        print(f"失败 {failed} 项")
        return 1
    print("存储层验证全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
