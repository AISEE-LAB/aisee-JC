"""
中转站分组倍率监测 - 主脚本

工作流程：
  1. 读取 config.yaml
  2. 拉取中转站的倍率数据（普通用户视角，多端点兼容）
  3. 与 state.json 里的上次快照比对
  4. 发现变化（或首次运行）→ 通过 notifiers 多渠道推送
  5. 写回 state.json

用法：
  python monitor.py            # 跑一次（计划任务调用）
  python monitor.py --once     # 同上，显式单次
  python monitor.py --loop     # 常驻模式，按 interval_minutes 循环
  python monitor.py --test     # 仅发一条测试通知，验证渠道
  python monitor.py --show     # 打印当前快照，不发通知
  python monitor.py --reset    # 清空 state.json（下次运行视为首次）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

import notifiers

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")

log = logging.getLogger("monitor")


# =========================================================
# 配置 & 日志
# =========================================================
def load_config(path: str) -> Dict[str, Any]:
    """读取 YAML 配置。
    - 文件不存在时抛 FileNotFoundError（Web 端可 try/except）。
    - 仅在作为脚本直接运行时（__main__）由 main() 捕获并 sys.exit。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"配置文件不存在：{path}（请复制 config.example.yaml 为 config.yaml 并填写）"
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def setup_logging(log_cfg: Dict[str, Any], force: bool = True) -> None:
    """配置根 logger。
    - force=True（脚本运行的默认）：用 basicConfig(force=True) 接管全局
    - force=False（Web 端调用）：避免覆盖 Flask/其它库的 logging 配置
    """
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    fh = log_cfg.get("file")
    if fh:
        if os.path.dirname(fh):
            os.makedirs(os.path.dirname(fh), exist_ok=True)
        handlers.append(logging.FileHandler(fh, encoding="utf-8"))
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, handlers=handlers, format=fmt, force=force)


# =========================================================
# 数据结构
# =========================================================
@dataclass
class Snapshot:
    """一次抓取得到的全量快照。
    - user_group:    当前账号所属分组（普通用户视角）
    - group_ratios:  {分组名: 倍率}（仅管理员/某些版本能拿到，普通用户可能为空）
    - model_ratios:  {模型名: {ratio, completion_ratio, model_price, group_ratio}}
    - raw_meta:      其它想记录的字段
    """
    fetched_at: float
    site: str
    user_group: str = ""
    group_ratios: Dict[str, float] = field(default_factory=dict)
    model_ratios: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    raw_meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "site": self.site,
            "user_group": self.user_group,
            "group_ratios": self.group_ratios,
            "model_ratios": self.model_ratios,
            "raw_meta": self.raw_meta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Snapshot":
        return cls(
            fetched_at=d.get("fetched_at", 0.0),
            site=d.get("site", ""),
            user_group=d.get("user_group", ""),
            group_ratios=d.get("group_ratios", {}),
            model_ratios=d.get("model_ratios", {}),
            raw_meta=d.get("raw_meta", {}),
        )


# =========================================================
# 抓取层（New API，普通用户视角，多端点兼容）
# =========================================================
class SiteClient:
    def __init__(self, site_cfg: Dict[str, Any]):
        self.base = (site_cfg.get("base_url") or "").rstrip("/")
        self.token = (site_cfg.get("access_token") or "").strip()
        self.cookie = (site_cfg.get("session_cookie") or "").strip()
        self.timeout = int(site_cfg.get("timeout", 15))
        self.retry = int(site_cfg.get("retry", 3))
        if not self.base:
            raise ValueError("site.base_url 未配置")

    def _headers(self) -> Dict[str, str]:
        h = {"User-Agent": "rate-monitor/1.0", "Accept": "application/json"}
        if self.token:
            # New API 同时支持 Authorization 和自定义头
            h["Authorization"] = f"Bearer {self.token}"
            h["New-Api-User"] = self.token
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        url = f"{self.base}{path}"
        last_err = None
        for attempt in range(1, self.retry + 1):
            try:
                r = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
                if r.status_code == 401:
                    log.error("鉴权失败（401），请检查 access_token / session_cookie")
                    return None
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    log.warning("GET %s -> %s (尝试 %s/%s)", path, last_err, attempt, self.retry)
                    time.sleep(1.5 * attempt)
                    continue
                try:
                    return r.json()
                except Exception:
                    last_err = f"非 JSON 响应：{r.text[:120]}"
                    log.warning("GET %s -> %s", path, last_err)
                    return None
            except requests.RequestException as e:
                last_err = repr(e)
                log.warning("GET %s 异常：%s (尝试 %s/%s)", path, last_err, attempt, self.retry)
                time.sleep(1.5 * attempt)
        log.error("GET %s 最终失败：%s", path, last_err)
        return None

    # ---------- 各端点 ----------
    def fetch_user_self(self) -> Optional[Dict]:
        data = self._get("/api/user/self")
        if not data:
            return None
        if not data.get("success", True):
            log.warning("user.self 返回失败：%s", data.get("message"))
            return None
        return data.get("data") or {}

    def fetch_pricing(self) -> Optional[List[Dict]]:
        """New API 公开定价接口，返回模型列表（含 model_ratio 等）。
        普通用户也可访问，是监测「实际单价变化」最可靠的端点。"""
        data = self._get("/api/pricing")
        if not data:
            return None
        if not data.get("success", True):
            log.warning("pricing 返回失败：%s", data.get("message"))
            return None
        # 不同版本字段名：data 或 data.models
        d = data.get("data")
        if isinstance(d, dict):
            return d.get("models") or []
        if isinstance(d, list):
            return d
        return []

    def fetch_option(self) -> Optional[Dict[str, Any]]:
        """管理端点：拿到 GroupRatio / UserUsableGroups 等完整配置。
        普通用户通常 401/无权限，返回 None 即可，不影响主流程。"""
        data = self._get("/api/option/")
        if not data:
            return None
        if not data.get("success", True):
            log.info("option 接口无权限（普通用户预期内），跳过分组倍率配置拉取")
            return None
        items = data.get("data") or []
        out: Dict[str, Any] = {}
        for it in items:
            k = it.get("key")
            v = it.get("value")
            if k:
                out[k] = v
        return out


# =========================================================
# 解析层：把原始 JSON 整理成 Snapshot
# =========================================================
def parse_group_ratio_option(value: str) -> Dict[str, float]:
    """GroupRatio 选项形如 '{"default":1,"vip":0.8}'。"""
    if not value:
        return {}
    try:
        return {k: float(v) for k, v in json.loads(value).items()}
    except Exception:
        return {}


def parse_model_ratio_option(value: str) -> Dict[str, float]:
    """ModelRatio 选项形如 '{"gpt-4o":2.5, ...}'。"""
    return parse_group_ratio_option(value)


def build_snapshot(client: SiteClient) -> Snapshot:
    snap = Snapshot(fetched_at=time.time(), site=client.base)

    # 1) 用户信息（拿到自己的分组）
    user = client.fetch_user_self()
    if user:
        snap.user_group = user.get("group") or user.get("group_name") or ""
        username = user.get("username") or user.get("display_name") or ""
        if username:
            snap.raw_meta["username"] = username
        log.info("当前账号分组：%s", snap.user_group or "(未知)")

    # 2) 公开定价（普通用户主监测对象）
    models = client.fetch_pricing() or []
    for m in models:
        name = m.get("model_name") or m.get("name") or m.get("id")
        if not name:
            continue
        snap.model_ratios[name] = {
            "ratio": _to_float(m.get("model_ratio") or m.get("quota_type")),
            "completion_ratio": _to_float(m.get("model_ratio_2") or m.get("completion_ratio")),
            "model_price": _to_float(m.get("model_price")),
            "group_ratio": _to_float(m.get("group_ratio")),
        }
    log.info("拉取到 %d 个模型的定价", len(snap.model_ratios))

    # 3) 后台 option（管理员才能拿到完整分组倍率；普通用户这里通常为空）
    opt = client.fetch_option()
    if opt:
        gr = parse_group_ratio_option(opt.get("GroupRatio", ""))
        if gr:
            snap.group_ratios = gr
            log.info("拉取到 %d 个分组倍率配置", len(gr))
        # 也保存模型倍率原配置，便于交叉验证
        mr = parse_model_ratio_option(opt.get("ModelRatio", ""))
        if mr:
            snap.raw_meta["ModelRatio_option"] = mr

    return snap


# =========================================================
# QAPI (sub2api / Unity2) 抓取构建
# =========================================================
def build_snapshot_qapi(client) -> Snapshot:
    """用 QapiClient 抓取，映射到统一的 Snapshot 结构。
    - group_ratios：每个分组的 rate_multiplier
    - model_ratios：模型名 → {group_ratio: None, model_price: usage.cost}（用 usage 间接反映）
    - 同时把模型清单（/v1/models）也填进 model_ratios，用于监测新增/下架
    """
    snap = Snapshot(fetched_at=time.time(), site=client.base)

    # 1) 用户信息
    try:
        user = client.fetch_user_profile() or {}
        snap.user_group = ",".join(user.get("allowed_groups") or []) or (user.get("group") or "")
        username = user.get("username") or user.get("email") or ""
        if username:
            snap.raw_meta["username"] = username
        log.info("当前账号：%s，可用分组：%s", username or "(未知)", snap.user_group or "(无)")
    except Exception as e:
        log.warning("获取 QAPI 用户信息失败：%s", e)

    # 2) 分组倍率（核心）
    try:
        groups = client.fetch_groups_available() or []
        for g in groups:
            name = g.get("name") or g.get("id") or ""
            if not name:
                continue
            rate = _to_float(g.get("rate_multiplier"))
            if rate is None:
                rate = 1.0  # 缺省按 1.0
            snap.group_ratios[name] = rate
            # 顺便记录分组详细信息到 raw_meta，方便排查
            snap.raw_meta.setdefault("groups_detail", []).append({
                "name": name,
                "platform": g.get("platform"),
                "rate_multiplier": rate,
                "subscription_type": g.get("subscription_type"),
            })
        log.info("拉取到 %d 个分组倍率", len(snap.group_ratios))
    except Exception as e:
        log.warning("获取 QAPI 分组倍率失败：%s", e)

    # 3) 模型清单（用于监测模型新增/下架）
    try:
        models = client.fetch_models() or []
        for name in models:
            # 模型清单不含价格，全部留空，但占位便于新增/下架监测
            snap.model_ratios[name] = {
                "ratio": None,
                "completion_ratio": None,
                "model_price": None,
                "group_ratio": None,
            }
        log.info("拉取到 %d 个模型（不含价格，仅监测增减）", len(snap.model_ratios))
    except Exception as e:
        log.warning("获取 QAPI 模型清单失败：%s", e)

    # 4) 已用模型花费（间接反映价格变化）
    try:
        usage = client.fetch_usage_models(days=30) or []
        for u in usage:
            name = u.get("model")
            if not name:
                continue
            cost = _to_float(u.get("cost"))
            actual_cost = _to_float(u.get("actual_cost"))
            snap.model_ratios.setdefault(name, {
                "ratio": None, "completion_ratio": None,
                "model_price": None, "group_ratio": None,
            })
            # 把花费塞进 model_price 字段，作为「实际单价趋势」的代理指标
            snap.model_ratios[name]["model_price"] = cost
            snap.model_ratios[name]["completion_ratio"] = actual_cost
        if usage:
            log.info("拉取到 %d 个已用模型的花费记录", len(usage))
    except Exception as e:
        log.warning("获取 QAPI 用量花费失败：%s", e)

    return snap


def build_snapshot_auto(site_cfg: Dict[str, Any]) -> Snapshot:
    """根据 site.system 选择 New API / QAPI 抓取。统一入口。"""
    system = (site_cfg.get("system") or "newapi").lower()
    if system in ("qapi", "sub2api", "unity2"):
        import qapi_client  # 延迟导入，避免 New API 用户强制依赖
        client = qapi_client.QapiClient(site_cfg)
        return build_snapshot_qapi(client)
    # 默认 New API
    client = SiteClient(site_cfg)
    return build_snapshot(client)


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


# =========================================================
# 比对层
# =========================================================
@dataclass
class Diff:
    kind: str            # group_ratio / model_ratio / model_added / model_removed / first_run
    key: str             # 分组名 或 模型名
    before: Any = None
    after: Any = None

    def line(self) -> str:
        if self.kind == "first_run":
            return f"(首次运行基准)"
        if self.kind == "group_ratio":
            return f"分组倍率 [{self.key}]：{self._fmt(self.before)} -> {self._fmt(self.after)}"
        if self.kind == "model_ratio":
            sub = self.key
            return f"模型 [{self.key}] {sub}：{self._fmt(self.before)} -> {self._fmt(self.after)}"
        if self.kind == "model_added":
            return f"模型新增 [{self.key}]：{self._fmt(self.after)}"
        if self.kind == "model_removed":
            return f"模型下架 [{self.key}]：(原 {self._fmt(self.before)})"
        return f"{self.kind} {self.key}"

    @staticmethod
    def _fmt(v: Any) -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:g}"
        return str(v)


def diff_snapshots(
    old: Optional[Snapshot],
    new: Snapshot,
    watch_groups: List[str],
    watch_models: List[str],
    threshold_pct: float,
) -> List[Diff]:
    diffs: List[Diff] = []

    if old is None:
        diffs.append(Diff("first_run", ""))
        return diffs

    def _pass_threshold(before: Optional[float], after: Optional[float]) -> bool:
        """过滤浮点抖动：变化幅度低于阈值则忽略。"""
        if threshold_pct <= 0:
            return True
        if before is None or after is None:
            return True  # 出现/消失一定要报
        if before == 0:
            return after != 0
        return abs(after - before) / abs(before) * 100 >= threshold_pct

    # ---- 分组倍率 ----
    g_old, g_new = old.group_ratios, new.group_ratios
    if g_old or g_new:
        keys = set(g_old) | set(g_new)
        for k in keys:
            if watch_groups and k not in watch_groups:
                continue
            b, a = g_old.get(k), g_new.get(k)
            if b != a and _pass_threshold(b, a):
                diffs.append(Diff("group_ratio", k, b, a))

    # ---- 模型定价 ----
    m_old, m_new = old.model_ratios, new.model_ratios
    keys = set(m_old) | set(m_new)
    for name in sorted(keys):
        if watch_models and not any(name.startswith(p) for p in watch_models):
            continue
        if name in m_old and name not in m_new:
            diffs.append(Diff("model_removed", name, m_old[name].get("ratio"), None))
            continue
        if name not in m_old and name in m_new:
            diffs.append(Diff("model_added", name, None, m_new[name].get("ratio")))
            continue
        # 都在 → 比对每个子字段
        for field_name in ("ratio", "completion_ratio", "model_price", "group_ratio"):
            b = m_old[name].get(field_name)
            a = m_new[name].get(field_name)
            if b != a and _pass_threshold(b, a):
                diffs.append(
                    Diff("model_ratio", f"{name}.{field_name}", b, a)
                )
    return diffs


# =========================================================
# 通知文案
# =========================================================
def build_message(
    site: str,
    diffs: List[Diff],
    new_snap: Snapshot,
    is_first: bool,
) -> Tuple[str, str]:
    """返回 (标题, markdown 正文)"""
    short = site.replace("https://", "").replace("http://", "").rstrip("/")
    if is_first:
        title = f"[倍率监测] 首次基准已建立 · {short}"
    else:
        title = f"[倍率监测] 检测到 {len(diffs)} 处变化 · {short}"

    lines: List[str] = []
    lines.append(f"**站点**：{site}")
    if new_snap.user_group:
        lines.append(f"**当前分组**：{new_snap.user_group}")
    lines.append(f"**抓取时间**：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(new_snap.fetched_at))}")
    lines.append("")

    if is_first:
        lines.append("本次为首次运行，已记录当前倍率为基准，后续变化才会告警。")
        lines.append("")
        # 顺手列一下当前关注的分组倍率，便于确认
        if new_snap.group_ratios:
            lines.append("**当前分组倍率**：")
            for g, r in new_snap.group_ratios.items():
                lines.append(f"- `{g}` × {r:g}")
            lines.append("")
        lines.append(f"**模型数**：{len(new_snap.model_ratios)} 个")
    else:
        if not diffs:
            lines.append("本轮无变化。")
        else:
            lines.append(f"**检测到 {len(diffs)} 处变化**：")
            lines.append("")
            for d in diffs:
                lines.append(f"- {d.line()}")

    return title, "\n".join(lines)


# =========================================================
# 状态持久化（按站点分文件，多站点版）
# =========================================================
def state_path_for(site_id: str) -> str:
    """每个站点一个 state 文件：state_<site_id>.json"""
    return os.path.join(SCRIPT_DIR, f"state_{site_id}.json")


def load_state(site_id: str) -> Optional[Snapshot]:
    path = state_path_for(site_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Snapshot.from_dict(json.load(f))
    except Exception as e:
        log.warning("[%s] state 读取失败，将视为首次：%s", site_id, e)
        return None


def save_state(site_id: str, snap: Snapshot) -> None:
    path = state_path_for(site_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _normalize_site_id(base_url: str) -> str:
    """与 db.normalize_site_id 保持一致的逻辑（CLI 不依赖 db 模块）。"""
    import re
    if not base_url:
        return "unknown"
    s = base_url.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]
    s = s.replace(":", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def get_sites_from_cfg(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从配置规范化出站点列表（兼容老 site: 单 dict 格式）。"""
    sites: List[Dict[str, Any]] = []
    if isinstance(cfg.get("sites"), list):
        sites = cfg["sites"]
    elif isinstance(cfg.get("site"), dict):
        old = cfg["site"]
        if "monitor" not in old and isinstance(cfg.get("monitor"), dict):
            old["monitor"] = cfg["monitor"]
        if "notify" not in old and isinstance(cfg.get("notify"), dict):
            old["notify"] = cfg["notify"]
        sites = [old]
    for s in sites:
        if not s.get("id"):
            s["id"] = _normalize_site_id(s.get("base_url", ""))
        s.setdefault("monitor", {"interval_minutes": 30, "change_threshold_pct": 0,
                                  "watch_groups": [], "watch_models": [], "notify_on_first_run": False})
        s.setdefault("notify", {})
    return sites


# =========================================================
# 主流程（多站点）
# =========================================================
def run_once_site(site_cfg: Dict[str, Any], force_notify: bool = False) -> int:
    """抓取单个站点。"""
    site_id = site_cfg.get("id") or _normalize_site_id(site_cfg.get("base_url", ""))
    mon_cfg = site_cfg.get("monitor", {})
    notify_cfg = site_cfg.get("notify", {})
    base_url = site_cfg.get("base_url", "")

    try:
        new_snap = build_snapshot_auto(site_cfg)
    except Exception as e:
        log.error("[%s] 抓取失败：%s", site_id, e)
        return 1

    if not new_snap.model_ratios and not new_snap.group_ratios:
        log.error("[%s] 未拉到任何数据", site_id)
        return 1

    old_snap = load_state(site_id)
    diffs = diff_snapshots(
        old_snap, new_snap,
        watch_groups=mon_cfg.get("watch_groups") or [],
        watch_models=mon_cfg.get("watch_models") or [],
        threshold_pct=float(mon_cfg.get("change_threshold_pct", 0) or 0),
    )

    is_first = old_snap is None
    should_notify = force_notify or is_first or bool(diffs)
    if is_first and not mon_cfg.get("notify_on_first_run", False):
        should_notify = force_notify

    save_state(site_id, new_snap)
    log.info("[%s] 快照已保存", site_id)

    if not should_notify:
        log.info("[%s] 无变化，不发通知", site_id)
        return 0

    title, content = build_message(base_url, diffs, new_snap, is_first)
    log.info("[%s] 发送通知：%s", site_id, title)
    results = notifiers.send_all(notify_cfg, title, content)
    for name, ok, msg in results:
        (log.info if ok else log.warning)("[%s] %s %s: %s", site_id, "✓" if ok else "✗", name, msg)
    if not results:
        log.warning("[%s] 未启用任何通知渠道", site_id)
    return 0


def run_once(cfg: Dict[str, Any], force_notify: bool = False) -> int:
    """遍历所有站点抓取。"""
    sites = get_sites_from_cfg(cfg)
    if not sites:
        log.error("配置里没有站点（sites 列表为空）")
        return 1
    log.info("开始抓取 %d 个站点", len(sites))
    rc = 0
    for s in sites:
        try:
            rc |= run_once_site(s, force_notify=force_notify)
        except Exception as e:
            log.exception("[%s] 异常", s.get("id"))
            rc = 1
    return rc


def run_loop(cfg: Dict[str, Any]) -> None:
    sites = get_sites_from_cfg(cfg)
    intervals = [int((s.get("monitor") or {}).get("interval_minutes", 30)) for s in sites]
    interval = min(intervals) if intervals else 30
    log.info("常驻模式：每 %d 分钟跑一轮（共 %d 个站点），Ctrl+C 退出", interval, len(sites))
    while True:
        try:
            run_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("运行异常：%s", e)
        log.info("--- 等待 %d 分钟 ---", interval)
        time.sleep(interval * 60)


def cmd_show(cfg: Dict[str, Any]) -> int:
    sites = get_sites_from_cfg(cfg)
    for s in sites:
        sid = s.get("id", "?")
        snap = load_state(sid)
        print(f"=== [{sid}] {s.get('base_url', '')} ===")
        if not snap:
            print("  （尚无快照）")
        else:
            print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_test(cfg: Dict[str, Any]) -> int:
    log.info("发送测试通知（所有站点）…")
    return run_once(cfg, force_notify=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="中转站分组倍率监测")
    parser.add_argument("--once", action="store_true", help="单次运行（默认）")
    parser.add_argument("--loop", action="store_true", help="常驻循环模式")
    parser.add_argument("--test", action="store_true", help="强制发一条通知，验证渠道")
    parser.add_argument("--show", action="store_true", help="打印当前快照")
    parser.add_argument("--reset", action="store_true", help="清空 state.json")
    args = parser.parse_args()

    cfg = load_config(CONFIG_PATH)
    setup_logging(cfg.get("log", {}), force=True)

    if args.reset:
        # 清空所有站点的 state 文件
        import glob
        state_files = glob.glob(os.path.join(SCRIPT_DIR, "state_*.json"))
        if not state_files and os.path.exists(STATE_PATH):
            state_files = [STATE_PATH]  # 老格式兼容
        for f in state_files:
            os.remove(f)
            log.info("已清空 %s", f)
        if not state_files:
            log.info("无 state 文件可清空")
        return 0
    if args.show:
        return cmd_show(cfg)
    if args.test:
        return cmd_test(cfg)
    if args.loop:
        run_loop(cfg)
        return 0
    return run_once(cfg)


if __name__ == "__main__":
    sys.exit(main())
