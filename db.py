"""
SQLite 数据访问层（多站点版）。
所有业务表都带 site_id 列，按站点隔离数据。
查询函数的 site_id 参数：传具体值 = 按站点过滤；传 None = 全站点汇总。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"))

_lock = threading.Lock()


# =========================================================
# 工具：site_id 规范化
# =========================================================
def normalize_site_id(base_url: str) -> str:
    """从 base_url 生成稳定的 site_id。
    https://qjxjs.xyz  ->  qjxjs_xyz
    http://api.example.com:8080/  ->  api_example_com_8080
    """
    if not base_url:
        return "unknown"
    s = base_url.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]  # 去掉路径
    s = s.replace(":", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


# =========================================================
# 初始化 + 迁移
# =========================================================
def init_db() -> None:
    """建表（IF NOT EXISTS）+ 老库迁移加 site_id 列。幂等。
    顺序：先建表(不含 site_id 索引) → 老库迁移 ALTER 加 site_id 列 → 最后建含 site_id 的索引。
    """
    with _lock, get_conn() as conn:
        c = conn.cursor()

        # 1) 建表（新库会带 site_id 列；老库已存在则 IF NOT EXISTS 跳过，列由迁移补）
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id          TEXT PRIMARY KEY,
                name        TEXT,
                base_url    TEXT,
                system      TEXT,
                created_at  REAL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at      REAL    NOT NULL,
                site            TEXT,
                site_id         TEXT,
                user_group      TEXT,
                group_ratios    TEXT,
                model_ratios    TEXT,
                raw_meta        TEXT,
                success         INTEGER NOT NULL DEFAULT 1,
                error_msg       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON snapshots(fetched_at DESC);

            CREATE TABLE IF NOT EXISTS model_ratios (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id       INTEGER NOT NULL,
                site_id           TEXT,
                fetched_at        REAL    NOT NULL,
                model_name        TEXT    NOT NULL,
                ratio             REAL,
                completion_ratio  REAL,
                model_price       REAL,
                group_ratio       REAL,
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_mr_snapshot ON model_ratios(snapshot_id);

            CREATE TABLE IF NOT EXISTS group_ratios (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                site_id     TEXT,
                fetched_at  REAL    NOT NULL,
                group_name  TEXT    NOT NULL,
                ratio       REAL,
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS changes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at  REAL    NOT NULL,
                snapshot_id  INTEGER,
                site_id      TEXT,
                kind         TEXT    NOT NULL,
                key_name     TEXT    NOT NULL,
                before_val   TEXT,
                after_val    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_changes_kind ON changes(kind);

            CREATE TABLE IF NOT EXISTS notifications (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at        REAL    NOT NULL,
                site_id        TEXT,
                title          TEXT,
                results        TEXT,
                changes_count  INTEGER DEFAULT 0,
                snapshot_id    INTEGER
            );

            CREATE TABLE IF NOT EXISTS run_logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL    NOT NULL,
                level   TEXT,
                site_id TEXT,
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_time ON run_logs(ts DESC);
            """
        )

        # 2) 老库迁移：补 site_id 列并回填
        _migrate_add_site_id(conn)

        # 3) 现在所有表都有 site_id 列了，补建含 site_id 的索引（IF NOT EXISTS 幂等）
        c.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshots_site ON snapshots(site_id, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_mr_name_time ON model_ratios(site_id, model_name, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_gr_name_time ON group_ratios(site_id, group_name, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_changes_time ON changes(site_id, detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notif_time ON notifications(site_id, sent_at DESC);
            """
        )


def _migrate_add_site_id(conn) -> None:
    """老库（单站点版）迁移：给业务表补 site_id 列，并从 snapshots.site 回填。"""
    tables_cols = {
        "snapshots": "site_id",
        "model_ratios": "site_id",
        "group_ratios": "site_id",
        "changes": "site_id",
        "notifications": "site_id",
        "run_logs": "site_id",
    }
    for table, col in tables_cols.items():
        # 检查列是否已存在
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    # 回填：snapshots 有 site(base_url)，转成 site_id
    rows = conn.execute("SELECT id, site FROM snapshots WHERE (site_id IS NULL OR site_id='') AND site IS NOT NULL").fetchall()
    for r in rows:
        sid = normalize_site_id(r["site"])
        conn.execute("UPDATE snapshots SET site_id=? WHERE id=?", (sid, r["id"]))
        # 级联到子表
        for sub in ("model_ratios", "group_ratios"):
            conn.execute(f"UPDATE {sub} SET site_id=? WHERE snapshot_id=? AND (site_id IS NULL OR site_id='')", (sid, r["id"]))
        for sub in ("changes", "notifications"):
            conn.execute(f"UPDATE {sub} SET site_id=? WHERE snapshot_id=? AND (site_id IS NULL OR site_id='')", (sid, r["id"]))


# =========================================================
# 站点管理
# =========================================================
def upsert_site(site_id: str, name: str, base_url: str, system: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO sites(id, name, base_url, system, created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, system=excluded.system",
            (site_id, name, base_url, system, time.time()),
        )


def get_sites() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sites ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def get_site(site_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    return dict(row) if row else None


# =========================================================
# 写入
# =========================================================
def insert_snapshot(site_id: str, snap_dict: Dict[str, Any], success: bool, error_msg: str = "") -> int:
    fetched_at = snap_dict.get("fetched_at") or time.time()
    group_ratios = snap_dict.get("group_ratios", {}) or {}
    model_ratios = snap_dict.get("model_ratios", {}) or {}
    raw_meta = snap_dict.get("raw_meta", {}) or {}

    with _lock, get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO snapshots(fetched_at, site, site_id, user_group, group_ratios, model_ratios, raw_meta, success, error_msg) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                fetched_at,
                snap_dict.get("site", ""),
                site_id,
                snap_dict.get("user_group", ""),
                json.dumps(group_ratios, ensure_ascii=False),
                json.dumps(model_ratios, ensure_ascii=False),
                json.dumps(raw_meta, ensure_ascii=False),
                1 if success else 0,
                error_msg,
            ),
        )
        sid = c.lastrowid

        if group_ratios:
            c.executemany(
                "INSERT INTO group_ratios(snapshot_id, site_id, fetched_at, group_name, ratio) VALUES (?,?,?,?,?)",
                [(sid, site_id, fetched_at, gn, _safe_float(r)) for gn, r in group_ratios.items()],
            )
        if model_ratios:
            rows = []
            for name, fields in model_ratios.items():
                if not isinstance(fields, dict):
                    continue
                rows.append(
                    (
                        sid, site_id, fetched_at, name,
                        _safe_float(fields.get("ratio")),
                        _safe_float(fields.get("completion_ratio")),
                        _safe_float(fields.get("model_price")),
                        _safe_float(fields.get("group_ratio")),
                    )
                )
            if rows:
                c.executemany(
                    "INSERT INTO model_ratios(snapshot_id, site_id, fetched_at, model_name, ratio, completion_ratio, model_price, group_ratio) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
        return sid


def insert_changes(site_id: str, snapshot_id: int, diffs: List[Any], detected_at: Optional[float] = None) -> int:
    if not diffs:
        return 0
    ts = detected_at or time.time()
    rows = []
    for d in diffs:
        kind = getattr(d, "kind", str(d))
        if kind == "first_run":
            continue
        key_name = getattr(d, "key", "")
        before = getattr(d, "before", None)
        after = getattr(d, "after", None)
        rows.append((ts, site_id, snapshot_id, kind, key_name, _val_to_str(before), _val_to_str(after)))
    if not rows:
        return 0
    with _lock, get_conn() as conn:
        conn.executemany(
            "INSERT INTO changes(detected_at, site_id, snapshot_id, kind, key_name, before_val, after_val) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def insert_notification(site_id: str, title: str, results: List[Tuple[str, bool, str]],
                         changes_count: int, snapshot_id: Optional[int]) -> int:
    with _lock, get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO notifications(sent_at, site_id, title, results, changes_count, snapshot_id) VALUES (?,?,?,?,?,?)",
            (
                time.time(), site_id, title,
                json.dumps([{"channel": n, "ok": ok, "msg": m} for n, ok, m in results], ensure_ascii=False),
                changes_count, snapshot_id,
            ),
        )
        return c.lastrowid


def insert_log(level: str, message: str, site_id: Optional[str] = None) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO run_logs(ts, level, site_id, message) VALUES (?,?,?,?)",
            (time.time(), level, site_id, message),
        )


def clear_changes(site_id: Optional[str] = None) -> int:
    """清空变更记录。传 site_id = 只清该站点；None = 全清。"""
    with _lock, get_conn() as conn:
        c = conn.cursor()
        if site_id:
            c.execute("DELETE FROM changes WHERE site_id=?", (site_id,))
        else:
            c.execute("DELETE FROM changes")
        return c.rowcount


# =========================================================
# 查询（全部支持 site_id 过滤；None = 全站点）
# =========================================================
def get_latest_snapshot(site_id: Optional[str] = None, include_fail: bool = False) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        q = "SELECT * FROM snapshots"
        clauses = []
        params: List[Any] = []
        if site_id:
            clauses.append("site_id = ?"); params.append(site_id)
        if not include_fail:
            clauses.append("success = 1")
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY fetched_at DESC LIMIT 1"
        row = conn.execute(q, params).fetchone()
        return _row_to_snapshot(row) if row else None


def get_latest_success_info(site_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        q = "SELECT fetched_at, success, error_msg, site_id FROM snapshots"
        if site_id:
            q += " WHERE site_id=?"
        q += " ORDER BY fetched_at DESC LIMIT 1"
        params = [site_id] if site_id else []
        row = conn.execute(q, params).fetchone()
        return dict(row) if row else None


def get_model_list(site_id: Optional[str] = None, search: str = "", sort: str = "name") -> List[Dict[str, Any]]:
    snap = get_latest_snapshot(site_id=site_id)
    if not snap:
        return []
    fetched_at = snap["fetched_at"]
    model_ratios: Dict[str, Dict[str, Any]] = snap["model_ratios"]

    name_filter = f"%{search}%" if search else "%"
    with get_conn() as conn:
        if site_id:
            last_changed_rows = conn.execute(
                """
                SELECT key_name, MAX(detected_at) AS last_changed
                FROM changes
                WHERE site_id=? AND kind IN ('model_ratio','model_added','model_removed') AND key_name LIKE ?
                GROUP BY key_name
                """,
                (site_id, name_filter),
            ).fetchall()
        else:
            last_changed_rows = conn.execute(
                """
                SELECT key_name, MAX(detected_at) AS last_changed
                FROM changes
                WHERE kind IN ('model_ratio','model_added','model_removed') AND key_name LIKE ?
                GROUP BY key_name
                """,
                (name_filter,),
            ).fetchall()
    last_changed_map = {r["key_name"]: r["last_changed"] for r in last_changed_rows}

    out = []
    for name, fields in model_ratios.items():
        if search and search.lower() not in name.lower():
            continue
        last_change = None
        for k, t in last_changed_map.items():
            model_part = k.split(".", 1)[0]
            if model_part == name:
                if last_change is None or t > last_change:
                    last_change = t
        out.append({
            "model_name": name,
            "ratio": fields.get("ratio"),
            "completion_ratio": fields.get("completion_ratio"),
            "model_price": fields.get("model_price"),
            "group_ratio": fields.get("group_ratio"),
            "last_changed_at": last_change,
            "fetched_at": fetched_at,
        })

    if sort == "ratio":
        out.sort(key=lambda x: (x["ratio"] is None, -(x["ratio"] or 0)))
    elif sort == "changed":
        out.sort(key=lambda x: (x["last_changed_at"] is None, -(x["last_changed_at"] or 0)))
    else:
        out.sort(key=lambda x: x["model_name"])
    return out


def get_model_history(model_name: str, site_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if site_id:
            rows = conn.execute(
                "SELECT fetched_at, ratio, completion_ratio, model_price, group_ratio "
                "FROM model_ratios WHERE model_name=? AND site_id=? ORDER BY fetched_at DESC LIMIT ?",
                (model_name, site_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT fetched_at, ratio, completion_ratio, model_price, group_ratio "
                "FROM model_ratios WHERE model_name=? ORDER BY fetched_at DESC LIMIT ?",
                (model_name, limit),
            ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    return out


def get_changes(site_id: Optional[str] = None, limit: int = 100, kind_filter: str = "") -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if site_id:
        clauses.append("site_id=?"); params.append(site_id)
    if kind_filter:
        clauses.append("kind=?"); params.append(kind_filter)
    q = "SELECT * FROM changes"
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY detected_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_notifications(site_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    q = "SELECT * FROM notifications"
    params: List[Any] = []
    if site_id:
        q += " WHERE site_id=?"; params.append(site_id)
    q += " ORDER BY sent_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["results"] = json.loads(d.get("results") or "[]")
        except Exception:
            d["results"] = []
        out.append(d)
    return out


def get_logs(site_id: Optional[str] = None, limit: int = 200, level: str = "") -> List[Dict[str, Any]]:
    # 日志默认全局（很多日志无 site_id，比如启动日志），传了也只作为可选过滤
    clauses = []
    params: List[Any] = []
    if level:
        clauses.append("level=?"); params.append(level)
    q = "SELECT * FROM run_logs"
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_stats(site_id: Optional[str] = None) -> Dict[str, Any]:
    clauses = []
    params: List[Any] = []
    if site_id:
        clauses.append("site_id=?"); params.append(site_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_conn() as conn:
        total_snaps = conn.execute(f"SELECT COUNT(*) AS c FROM snapshots{where}", params).fetchone()["c"]
        success_snaps = conn.execute(
            f"SELECT COUNT(*) AS c FROM snapshots{where + ' AND ' if where else ' WHERE '}success=1",
            params,
        ).fetchone()["c"] if where else conn.execute("SELECT COUNT(*) AS c FROM snapshots WHERE success=1").fetchone()["c"]
        total_changes = conn.execute(f"SELECT COUNT(*) AS c FROM changes{where}", params).fetchone()["c"]
        last_24h_changes = conn.execute(
            f"SELECT COUNT(*) AS c FROM changes{where + ' AND ' if where else ' WHERE '}detected_at >= ?",
            params + [time.time() - 86400],
        ).fetchone()["c"] if where else conn.execute(
            "SELECT COUNT(*) AS c FROM changes WHERE detected_at >= ?", (time.time() - 86400,)
        ).fetchone()["c"]
        total_notifs = conn.execute(f"SELECT COUNT(*) AS c FROM notifications{where}", params).fetchone()["c"]
    return {
        "total_snapshots": total_snaps,
        "success_snapshots": success_snaps,
        "failed_snapshots": total_snaps - success_snaps,
        "total_changes": total_changes,
        "changes_last_24h": last_24h_changes,
        "total_notifications": total_notifs,
    }


def get_site_overview(site_id: str) -> Dict[str, Any]:
    """单站点概览（仪表盘汇总用）。"""
    snap = get_latest_snapshot(site_id=site_id)
    success_info = get_latest_success_info(site_id=site_id)
    stats = get_stats(site_id=site_id)
    changes_recent = get_changes(site_id=site_id, limit=3)
    return {
        "site_id": site_id,
        "latest_snapshot": _serialize_snapshot(snap),
        "latest_fetch": {
            "fetched_at": success_info["fetched_at"] if success_info else None,
            "success": bool(success_info["success"]) if success_info else None,
            "error_msg": success_info["error_msg"] if success_info else None,
        },
        "stats": stats,
        "changes_recent_count": len(changes_recent),
    }


# =========================================================
# 工具
# =========================================================
def _row_to_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["group_ratios"] = json.loads(d.get("group_ratios") or "{}")
    except Exception:
        d["group_ratios"] = {}
    try:
        d["model_ratios"] = json.loads(d.get("model_ratios") or "{}")
    except Exception:
        d["model_ratios"] = {}
    try:
        d["raw_meta"] = json.loads(d.get("raw_meta") or "{}")
    except Exception:
        d["raw_meta"] = {}
    return d


def _serialize_snapshot(snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not snap:
        return None
    return {
        "id": snap.get("id"),
        "fetched_at": snap.get("fetched_at"),
        "site": snap.get("site"),
        "site_id": snap.get("site_id"),
        "user_group": snap.get("user_group"),
        "group_ratios": snap.get("group_ratios", {}),
        "model_count": len(snap.get("model_ratios", {})),
        "group_count": len(snap.get("group_ratios", {})),
        "success": bool(snap.get("success", 1)),
        "error_msg": snap.get("error_msg", ""),
    }


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _val_to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)
