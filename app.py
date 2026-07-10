"""
中转站倍率监测 - Web 后端（多站点版）

- Flask 提供 5 个页面 + API
- APScheduler 后台定时抓取所有站点
- 复用 monitor.py / qapi_client.py / notifiers.py
- SQLite 持久化，按 site_id 隔离
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, render_template, request

import config_helper as ch
import db
import monitor
import notifiers

app = Flask(__name__)

log = logging.getLogger("monitor")
_scheduler: Optional[BackgroundScheduler] = None
_fetch_lock = threading.Lock()  # 同一时刻只允许一轮全站点抓取
_next_run_ts: Optional[float] = None


# =========================================================
# 日志
# =========================================================
class DBLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            db.insert_log(record.levelname, self.format(record))
        except Exception:
            pass


def init_logging() -> None:
    try:
        cfg = ch.load_config_raw()
    except Exception:
        cfg = {}
    monitor.setup_logging(cfg.get("log", {}) or {}, force=True)
    root = logging.getLogger()
    if not any(isinstance(h, DBLogHandler) for h in root.handlers):
        h = DBLogHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root.addHandler(h)


# =========================================================
# 访问鉴权（Basic Auth / API Token，可配置开关）
# =========================================================
AUTH_SECRET_KEYS = {"password", "token"}  # 保存时回填用的敏感字段


def get_auth_cfg() -> Dict[str, Any]:
    """读取 auth 配置并补默认值。enabled 默认 False，保证向后兼容。"""
    try:
        raw = ch.load_config_raw()
    except Exception:
        raw = {}
    auth = raw.get("auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    auth.setdefault("enabled", False)
    auth.setdefault("username", "admin")
    auth.setdefault("password", "")
    auth.setdefault("token", "")
    return auth


def mask_auth_cfg(auth: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏副本：密码/令牌非空 → ***"""
    out = dict(auth)
    for k in AUTH_SECRET_KEYS:
        if out.get(k):
            out[k] = "***"
    return out


def restore_auth_secrets(new_auth: Dict[str, Any], old_auth: Dict[str, Any]) -> None:
    """前端发回的 *** 视为「不改」，用旧值回填。就地修改。"""
    for k in AUTH_SECRET_KEYS:
        if new_auth.get(k) == "***":
            new_auth[k] = old_auth.get(k, "")


def check_auth() -> bool:
    """校验当前请求是否通过鉴权。未启用鉴权时直接放行。
    支持两种方式：
    1) HTTP Basic Auth（username + password）
    2) X-Api-Token 请求头或 ?api_token= 查询参数（token）
    任一通过即可。
    """
    auth_cfg = get_auth_cfg()
    if not auth_cfg.get("enabled"):
        return True
    user = auth_cfg.get("username") or "admin"
    pwd = auth_cfg.get("password") or ""
    token = (auth_cfg.get("token") or "").strip()

    # 方式 2：Token（优先，便于脚本/外部调用）
    if token:
        req_token = request.headers.get("X-Api-Token") or request.args.get("api_token") or ""
        if req_token and req_token == token:
            return True

    # 方式 1：Basic Auth
    if pwd:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Basic "):
            import base64 as _b64
            try:
                decoded = _b64.b64decode(auth_hdr[6:]).decode("utf-8")
                u, _, p = decoded.partition(":")
                if u == user and p == pwd:
                    return True
            except Exception:
                pass
    return False


@app.before_request
def _require_auth() -> Any:
    """全局鉴权钩子。未通过返回 401，浏览器会弹出 Basic Auth 登录框。
    /api/health 与静态资源免鉴权，供健康检查与容器探针使用。
    """
    # 健康检查端点免鉴权
    if request.path == "/api/health":
        return None
    if check_auth():
        return None
    # 浏览器访问返回 401 + WWW-Authenticate 触发登录框；API 调用返回 JSON
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "未授权：需要登录或提供有效 token"}), 401
    resp = app.make_response(("Unauthorized", 401))
    resp.headers["WWW-Authenticate"] = 'Basic realm="rate-monitor"'
    return resp


@app.route("/api/health")
def api_health():
    """健康检查端点（免鉴权）。供 Docker healthcheck / uptime 监控使用。"""
    try:
        sites = ch.get_sites_cfg()
        return jsonify({"ok": True, "status": "healthy", "sites": len(sites)}), 200
    except Exception as e:
        return jsonify({"ok": False, "status": "unhealthy", "error": str(e)}), 500


@app.route("/api/auth")
def api_auth_status():
    """返回当前鉴权状态（是否启用、是否已登录）。不含敏感字段。"""
    auth_cfg = get_auth_cfg()
    return jsonify({
        "enabled": bool(auth_cfg.get("enabled")),
        "username": auth_cfg.get("username", "admin"),
        "has_password": bool(auth_cfg.get("password")),
        "has_token": bool(auth_cfg.get("token")),
        "logged_in": check_auth() or not auth_cfg.get("enabled"),
    })


# =========================================================
# 配置：脱敏（多站点版）
# =========================================================
SITE_SECRET_KEYS = {"access_token", "session_cookie", "password", "api_key"}
NOTIFY_SECRET_KEYS = {
    "serverchan": {"sendkey"},
    "telegram": {"bot_token"},
    "wecom": {"webhook"},
    "dingtalk": {"webhook", "secret"},
    "email": {"smtp_pass"},
    "bark": {"device_key"},
    "discord": {"webhook"},
    "feishu": {"webhook", "secret"},
}


def mask_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏副本：sites[*] 和 notify 的敏感字段非空→***"""
    import json
    out = json.loads(json.dumps(cfg))  # deep copy
    for site in out.get("sites", []) or []:
        for k in SITE_SECRET_KEYS:
            if site.get(k):
                site[k] = "***"
        for ch_name, keys in NOTIFY_SECRET_KEYS.items():
            body = site.get("notify", {}).get(ch_name, {})
            for k in keys:
                if body.get(k):
                    body[k] = "***"
    # 兼容老格式 site:（如果还在用）
    if isinstance(out.get("site"), dict):
        for k in SITE_SECRET_KEYS:
            if out["site"].get(k):
                out["site"][k] = "***"
    return out


def restore_secrets(new_cfg: Dict[str, Any], old_cfg: Dict[str, Any]) -> None:
    """前端发回的 *** 视为「不改」，用旧值回填。就地修改 new_cfg。"""
    for i, site in enumerate(new_cfg.get("sites", []) or []):
        old_site = (old_cfg.get("sites") or [{}])[i] if i < len(old_cfg.get("sites") or []) else {}
        for k in SITE_SECRET_KEYS:
            if site.get(k) == "***":
                site[k] = old_site.get(k, "")
        for ch_name, keys in NOTIFY_SECRET_KEYS.items():
            body_new = site.get("notify", {}).get(ch_name, {})
            body_old = (old_site.get("notify") or {}).get(ch_name, {})
            for k in keys:
                if body_new.get(k) == "***":
                    body_new[k] = body_old.get(k, "")


# =========================================================
# 抓取编排
# =========================================================
def fetch_one_site(site_cfg: Dict[str, Any], force_notify: bool = False) -> Dict[str, Any]:
    """抓取单个站点。返回 {ok, site_id, snapshot_id, diffs_count, notified, error}"""
    base_url = (site_cfg.get("base_url") or "").rstrip("/")
    site_id = site_cfg.get("id") or db.normalize_site_id(base_url)
    mon_cfg = site_cfg.get("monitor", {}) or {}
    notify_cfg = site_cfg.get("notify", {}) or {}

    # 站点信息入库（保证 sites 表有记录，配置页才能展示）
    db.upsert_site(site_id, site_cfg.get("name", "") or base_url, base_url, site_cfg.get("system", ""))

    try:
        snap = monitor.build_snapshot_auto(site_cfg)
    except Exception as e:
        log.error("[%s] 抓取失败：%s", site_id, e)
        db.insert_log("ERROR", f"[{site_id}] 抓取异常：{e}", site_id=site_id)
        empty = {"fetched_at": time.time(), "site": base_url, "user_group": "",
                 "group_ratios": {}, "model_ratios": {}, "raw_meta": {}}
        db.insert_snapshot(site_id, empty, success=False, error_msg=str(e))
        return {"ok": False, "site_id": site_id, "error": str(e)}

    # 空数据保护
    if not snap.model_ratios and not snap.group_ratios:
        msg = "未拉到任何模型或分组数据"
        log.error("[%s] %s", site_id, msg)
        db.insert_log("ERROR", f"[{site_id}] {msg}", site_id=site_id)
        empty = {"fetched_at": snap.fetched_at, "site": base_url, "user_group": snap.user_group,
                 "group_ratios": {}, "model_ratios": {}, "raw_meta": snap.raw_meta}
        db.insert_snapshot(site_id, empty, success=False, error_msg=msg)
        return {"ok": False, "site_id": site_id, "error": msg}

    # 比对
    prev_snap_dict = db.get_latest_snapshot(site_id=site_id)
    prev_snap = monitor.Snapshot.from_dict(prev_snap_dict) if prev_snap_dict else None
    diffs = monitor.diff_snapshots(
        prev_snap, snap,
        watch_groups=mon_cfg.get("watch_groups") or [],
        watch_models=mon_cfg.get("watch_models") or [],
        threshold_pct=float(mon_cfg.get("change_threshold_pct", 0) or 0),
    )
    is_first = prev_snap is None

    # 写入
    snap_id = db.insert_snapshot(site_id, snap.to_dict(), success=True)
    inserted_changes = db.insert_changes(site_id, snap_id, diffs)

    # 决定是否通知
    should_notify = force_notify or bool(diffs)
    if is_first and not mon_cfg.get("notify_on_first_run", False):
        should_notify = force_notify

    notified_results: List[Tuple[str, bool, str]] = []
    title = ""
    if should_notify:
        title, content = monitor.build_message(base_url, diffs, snap, is_first or force_notify)
        log.info("[%s] 发送通知：%s", site_id, title)
        notified_results = notifiers.send_all(notify_cfg, title, content)
        db.insert_notification(site_id, title, notified_results, len(diffs), snap_id)
        for name, ok, msg in notified_results:
            (log.info if ok else log.warning)("[%s] %s %s: %s", site_id, "✓" if ok else "✗", name, msg)

    log.info("[%s] 抓取完成：模型=%d 变更=%d 通知=%s",
             site_id, len(snap.model_ratios), inserted_changes, "已发" if notified_results else "无")
    return {
        "ok": True, "site_id": site_id, "snapshot_id": snap_id,
        "model_count": len(snap.model_ratios), "group_count": len(snap.group_ratios),
        "diffs_count": len(diffs), "is_first": is_first,
        "notified": bool(notified_results),
        "notification_results": [{"channel": n, "ok": ok, "msg": m} for n, ok, m in notified_results],
    }


def fetch_all(force_notify: bool = False, only_site_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """遍历所有站点抓取。返回每个站点的结果列表。"""
    if not _fetch_lock.acquire(blocking=False):
        log.warning("已有抓取任务进行中，跳过")
        return [{"ok": False, "error": "已有抓取任务进行中"}]
    try:
        sites = ch.get_sites_cfg()
        if only_site_id:
            sites = [s for s in sites if s.get("id") == only_site_id]
            if not sites:
                return [{"ok": False, "error": f"未找到站点 {only_site_id}"}]
        results = []
        for s in sites:
            try:
                results.append(fetch_one_site(s, force_notify=force_notify))
            except Exception as e:
                log.exception("[%s] 抓取异常", s.get("id"))
                results.append({"ok": False, "site_id": s.get("id"), "error": str(e)})
        return results
    finally:
        _fetch_lock.release()


# =========================================================
# 调度器
# =========================================================
def _scheduled_fetch_all() -> None:
    global _next_run_ts
    try:
        fetch_all()
    except Exception as e:
        log.exception("定时抓取异常：%s", e)
    finally:
        _update_next_run()


def _update_next_run() -> None:
    global _next_run_ts
    if _scheduler is None:
        _next_run_ts = None
        return
    try:
        jobs = _scheduler.get_jobs()
        _next_run_ts = jobs[0].next_run_time.timestamp() if jobs else None
    except Exception:
        _next_run_ts = None


def _min_interval(sites: List[Dict[str, Any]]) -> int:
    intervals = [int((s.get("monitor") or {}).get("interval_minutes", 30) or 30) for s in sites]
    return max(1, min(intervals)) if intervals else 30


def init_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    sites = ch.get_sites_cfg()
    interval = _min_interval(sites)
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _scheduled_fetch_all,
        trigger=IntervalTrigger(minutes=interval),
        id="fetch_job",
        next_run_time=datetime.now(),
        max_instances=1, coalesce=True,
    )
    sched.start()
    _scheduler = sched
    _update_next_run()
    log.info("已启动定时调度，最小间隔 %d 分钟（按各站点 interval 取最小值）", interval)


def reschedule() -> None:
    global _scheduler
    if _scheduler is None:
        init_scheduler()
        return
    sites = ch.get_sites_cfg()
    interval = _min_interval(sites)
    try:
        _scheduler.reschedule_job("fetch_job", trigger=IntervalTrigger(minutes=interval))
        log.info("重设调度间隔：%d 分钟", interval)
        _update_next_run()
    except Exception as e:
        log.warning("重设调度失败：%s", e)


# =========================================================
# 页面
# =========================================================
@app.route("/")
def page_dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/models")
def page_models():
    return render_template("models.html", active="models")


@app.route("/changes")
def page_changes():
    return render_template("changes.html", active="changes")


@app.route("/config")
def page_config():
    return render_template("config.html", active="config")


@app.route("/logs")
def page_logs():
    return render_template("logs.html", active="logs")


# =========================================================
# API
# =========================================================
@app.route("/api/dashboard")
def api_dashboard():
    site_id = request.args.get("site_id")
    sites_cfg = ch.get_sites_cfg()
    sites_meta = db.get_sites()

    if site_id and site_id != "all":
        # 单站点视角
        snap = db.get_latest_snapshot(site_id=site_id)
        success_info = db.get_latest_success_info(site_id=site_id)
        stats = db.get_stats(site_id=site_id)
        changes_recent = db.get_changes(site_id=site_id, limit=5)
        notifs_recent = db.get_notifications(site_id=site_id, limit=3)
        site_cfg = next((s for s in sites_cfg if s.get("id") == site_id), {})
        interval = int((site_cfg.get("monitor") or {}).get("interval_minutes", 30))
        enabled_channels = [k for k, v in (site_cfg.get("notify") or {}).items() if isinstance(v, dict) and v.get("enabled")]
        return jsonify({
            "mode": "single",
            "site_id": site_id,
            "latest_snapshot": db._serialize_snapshot(snap),
            "latest_fetch": {
                "fetched_at": success_info["fetched_at"] if success_info else None,
                "success": bool(success_info["success"]) if success_info else None,
                "error_msg": success_info["error_msg"] if success_info else None,
            },
            "stats": stats,
            "changes_recent": [_serialize_change(c) for c in changes_recent],
            "notifications_recent": [_serialize_notif(n) for n in notifs_recent],
            "interval_minutes": interval,
            "next_run_ts": _next_run_ts,
            "enabled_channels": enabled_channels,
            "site": site_cfg.get("base_url", ""),
        })

    # 全局视角：所有站点概览
    overall_stats = db.get_stats()
    site_summaries = []
    for s in sites_meta:
        ov = db.get_site_overview(s["id"])
        site_cfg = next((c for c in sites_cfg if c.get("id") == s["id"]), {})
        site_summaries.append({
            "site_id": s["id"],
            "name": s.get("name") or s.get("base_url"),
            "base_url": s.get("base_url"),
            "system": s.get("system"),
            "latest_snapshot": ov["latest_snapshot"],
            "latest_fetch": ov["latest_fetch"],
            "stats": ov["stats"],
            "interval_minutes": int((site_cfg.get("monitor") or {}).get("interval_minutes", 30)),
        })
    # 同时包含配置里存在但 DB 还没记录的站点（从未抓取过）
    existing_ids = {s["site_id"] for s in site_summaries}
    for c in sites_cfg:
        if c.get("id") not in existing_ids:
            site_summaries.append({
                "site_id": c["id"],
                "name": c.get("name") or c.get("base_url"),
                "base_url": c.get("base_url"),
                "system": c.get("system"),
                "latest_snapshot": None,
                "latest_fetch": {"fetched_at": None, "success": None, "error_msg": None},
                "stats": {"total_snapshots": 0, "success_snapshots": 0, "failed_snapshots": 0,
                          "total_changes": 0, "changes_last_24h": 0, "total_notifications": 0},
                "interval_minutes": int((c.get("monitor") or {}).get("interval_minutes", 30)),
            })
    return jsonify({
        "mode": "overview",
        "sites": site_summaries,
        "overall": overall_stats,
        "next_run_ts": _next_run_ts,
        "recent_changes_global": [_serialize_change(c) for c in db.get_changes(limit=8)],
    })


@app.route("/api/models")
def api_models():
    site_id = request.args.get("site_id")
    if site_id == "all":
        site_id = None
    q = (request.args.get("q") or "").strip()
    sort = (request.args.get("sort") or "name").strip()
    if sort not in ("name", "ratio", "changed"):
        sort = "name"
    rows = db.get_model_list(site_id=site_id, search=q, sort=sort)
    return jsonify({"site_id": site_id, "models": rows, "count": len(rows)})


@app.route("/api/models/<path:model_name>/history")
def api_model_history(model_name: str):
    site_id = request.args.get("site_id")
    if site_id == "all":
        site_id = None
    limit = min(int(request.args.get("limit", 200)), 1000)
    rows = db.get_model_history(model_name, site_id=site_id, limit=limit)
    return jsonify({"model": model_name, "site_id": site_id, "history": rows})


@app.route("/api/changes")
def api_changes():
    site_id = request.args.get("site_id")
    if site_id == "all":
        site_id = None
    limit = min(int(request.args.get("limit", 100)), 1000)
    kind = (request.args.get("kind") or "").strip()
    rows = db.get_changes(site_id=site_id, limit=limit, kind_filter=kind)
    return jsonify({"site_id": site_id, "changes": [_serialize_change(c) for c in rows]})


@app.route("/api/notifications")
def api_notifications():
    site_id = request.args.get("site_id")
    if site_id == "all":
        site_id = None
    limit = min(int(request.args.get("limit", 20)), 200)
    rows = db.get_notifications(site_id=site_id, limit=limit)
    return jsonify({"site_id": site_id, "notifications": [_serialize_notif(n) for n in rows]})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    raw = ch.load_config_raw()
    sites = ch.normalize_sites(raw)
    auth = get_auth_cfg()
    # 重组返回结构：返回规范化后的 sites + log + auth（脱敏）
    return jsonify({
        "config": {
            "sites": sites,
            "log": raw.get("log", {"level": "INFO", "file": ""}),
            "auth": mask_auth_cfg(auth),
        },
        "has_yaml": os.path.exists(ch.CONFIG_PATH),
    })


@app.route("/api/config", methods=["POST"])
def api_config_save():
    data = request.get_json(silent=True) or {}
    new_sites = data.get("sites")
    if not isinstance(new_sites, list):
        return jsonify({"ok": False, "error": "sites 字段缺失或非列表"}), 400

    try:
        old_raw = ch.load_config_raw()
        old_sites = ch.normalize_sites(old_raw)
    except Exception:
        old_sites = []

    # auth 段处理：回填 *** 凭证
    old_auth = get_auth_cfg()
    new_auth = data.get("auth") or {}
    if not isinstance(new_auth, dict):
        new_auth = {}
    new_auth.setdefault("enabled", False)
    new_auth.setdefault("username", "admin")
    new_auth.setdefault("password", "")
    new_auth.setdefault("token", "")
    restore_auth_secrets(new_auth, old_auth)

    # 重组 new_cfg，保留 log + auth
    new_cfg = {
        "sites": new_sites,
        "log": data.get("log") or old_raw.get("log", {"level": "INFO", "file": ""}),
        "auth": new_auth,
    }
    # 回填 *** 凭证
    restore_secrets(new_cfg, {"sites": old_sites})

    try:
        ch.save_config_raw(new_cfg)
        log.info("配置已保存（%d 个站点，鉴权=%s）", len(new_sites), "开" if new_auth.get("enabled") else "关")
        reschedule()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("保存配置失败：%s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sites", methods=["GET"])
def api_sites_list():
    sites = ch.get_sites_cfg()
    return jsonify({"sites": [{"id": s["id"], "name": s.get("name") or s["base_url"],
                                "base_url": s["base_url"], "system": s.get("system", "")} for s in sites]})


@app.route("/api/sites", methods=["POST"])
def api_sites_add():
    data = request.get_json(silent=True) or {}
    base_url = (data.get("base_url") or "").strip()
    if not base_url:
        return jsonify({"ok": False, "error": "base_url 必填"}), 400
    system = data.get("system") or "newapi"
    new_site = ch.make_new_site(base_url, system=system, name=data.get("name", ""))
    sites = ch.get_sites_cfg()
    # 检查重复
    if any(s["id"] == new_site["id"] for s in sites):
        return jsonify({"ok": False, "error": f"站点 {new_site['id']} 已存在（base_url 重复）"}), 400
    sites.append(new_site)
    ch.save_config_raw({"sites": sites, "log": ch.load_config_raw().get("log", {})})
    reschedule()
    return jsonify({"ok": True, "site": new_site})


@app.route("/api/sites/<site_id>", methods=["DELETE"])
def api_sites_delete(site_id: str):
    sites = ch.get_sites_cfg()
    new_sites = [s for s in sites if s.get("id") != site_id]
    if len(new_sites) == len(sites):
        return jsonify({"ok": False, "error": "站点不存在"}), 404
    ch.save_config_raw({"sites": new_sites, "log": ch.load_config_raw().get("log", {})})
    reschedule()
    return jsonify({"ok": True, "deleted": site_id})


@app.route("/api/test_notify", methods=["POST"])
def api_test_notify():
    data = request.get_json(silent=True) or {}
    site_id = data.get("site_id")
    if not site_id:
        return jsonify({"ok": False, "error": "必须指定 site_id"}), 400
    log.info("[%s] 用户触发：测试通知", site_id)
    results = fetch_all(force_notify=True, only_site_id=site_id)
    return jsonify(results[0] if results else {"ok": False, "error": "无结果"})


@app.route("/api/fetch_now", methods=["POST"])
def api_fetch_now():
    data = request.get_json(silent=True) or {}
    site_id = data.get("site_id")
    log.info("用户触发：立即抓取 %s", site_id or "(全部)")
    results = fetch_all(force_notify=False, only_site_id=site_id)
    _update_next_run()
    return jsonify({"results": results})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    data = request.get_json(silent=True) or {}
    site_id = data.get("site_id")
    n = db.clear_changes(site_id=site_id)
    log.info("用户触发：重置基准（%s，清空 %d 条变更）", site_id or "全部", n)
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/logs")
def api_logs():
    limit = min(int(request.args.get("limit", 200)), 1000)
    level = (request.args.get("level") or "").strip()
    rows = db.get_logs(limit=limit, level=level)
    return jsonify({"logs": [_serialize_log(r) for r in rows]})


# =========================================================
# 序列化
# =========================================================
def _serialize_change(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": c.get("id"),
        "detected_at": c.get("detected_at"),
        "site_id": c.get("site_id"),
        "kind": c.get("kind"),
        "key_name": c.get("key_name"),
        "before_val": c.get("before_val"),
        "after_val": c.get("after_val"),
    }


def _serialize_notif(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": n.get("id"),
        "sent_at": n.get("sent_at"),
        "site_id": n.get("site_id"),
        "title": n.get("title"),
        "results": n.get("results", []),
        "changes_count": n.get("changes_count", 0),
    }


def _serialize_log(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r.get("id"),
        "ts": r.get("ts"),
        "level": r.get("level"),
        "site_id": r.get("site_id"),
        "message": r.get("message"),
    }


# =========================================================
# 入口
# =========================================================
def main() -> None:
    db.init_db()
    init_logging()
    log.info("=" * 50)
    log.info("中转站倍率监测 Web 服务启动（多站点版）")
    sites = ch.get_sites_cfg()
    log.info("配置了 %d 个站点：%s", len(sites), [s.get("id") for s in sites])
    if not os.path.exists(ch.CONFIG_PATH):
        log.warning("未发现 config.yaml，使用模板默认值")
    init_scheduler()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    log.info("访问地址：http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
