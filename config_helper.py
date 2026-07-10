"""配置处理：多站点结构 + 老 site: 的兼容转换。
被 app.py 和 monitor.py 共用，避免重复逻辑。
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List

import yaml

import db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 支持环境变量覆盖配置路径（Docker 部署把 config.yaml 放持久化卷里）
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(SCRIPT_DIR, "config.yaml"))
CONFIG_EXAMPLE = os.path.join(SCRIPT_DIR, "config.example.yaml")


# 默认 monitor 配置（新站点用）
DEFAULT_MONITOR = {
    "interval_minutes": 30,
    "change_threshold_pct": 0,
    "watch_groups": [],
    "watch_models": [],
    "notify_on_first_run": False,
}

# 默认 notify 配置（新站点用，全部 enabled=false）
DEFAULT_NOTIFY = {
    "serverchan": {"enabled": False, "sendkey": ""},
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "wecom": {"enabled": False, "webhook": ""},
    "dingtalk": {"enabled": False, "webhook": "", "secret": ""},
    "email": {
        "enabled": False,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_user": "",
        "smtp_pass": "",
        "from_addr": "",
        "to_addrs": [],
    },
    "bark": {
        "enabled": False,
        "server": "https://api.day.app",
        "device_key": "",
        "sound": "alert",
        "group": "rate-monitor",
        "icon": "",
    },
    "discord": {"enabled": False, "webhook": "", "username": "倍率监测"},
    "feishu": {"enabled": False, "webhook": "", "secret": ""},
}


def load_config_raw() -> Dict[str, Any]:
    """读原始 YAML（不做转换）。文件不存在用 example。"""
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else CONFIG_EXAMPLE
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config_raw(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def normalize_sites(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把配置里的 site/sites 规范化成 sites 列表。
    - 新格式 sites: [...] 直接返回
    - 老格式 site: {...} 自动包成单元素列表
    - 每个站点补全 monitor / notify 默认值
    - 自动生成 site_id（如果缺）
    """
    sites: List[Dict[str, Any]] = []

    if isinstance(cfg.get("sites"), list):
        sites = copy.deepcopy(cfg["sites"])
    elif isinstance(cfg.get("site"), dict):
        # 老格式兼容：site dict → 单站点
        old = copy.deepcopy(cfg["site"])
        # 全局 monitor / notify（老格式可能是全局的）下放到站点
        if "monitor" not in old and isinstance(cfg.get("monitor"), dict):
            old["monitor"] = copy.deepcopy(cfg["monitor"])
        if "notify" not in old and isinstance(cfg.get("notify"), dict):
            old["notify"] = copy.deepcopy(cfg["notify"])
        sites = [old]

    for s in sites:
        # 生成 site_id
        if not s.get("id"):
            s["id"] = db.normalize_site_id(s.get("base_url", ""))
        # 补默认值
        if "monitor" not in s or not isinstance(s.get("monitor"), dict):
            s["monitor"] = copy.deepcopy(DEFAULT_MONITOR)
        else:
            # 合并默认值（避免缺字段）
            merged = copy.deepcopy(DEFAULT_MONITOR)
            merged.update(s["monitor"])
            s["monitor"] = merged
        if "notify" not in s or not isinstance(s.get("notify"), dict):
            s["notify"] = copy.deepcopy(DEFAULT_NOTIFY)
        else:
            merged = copy.deepcopy(DEFAULT_NOTIFY)
            for ch, body in s["notify"].items():
                if ch in merged and isinstance(body, dict):
                    merged[ch].update(body)
            s["notify"] = merged
        # 显示名默认用 base_url
        if not s.get("name"):
            s["name"] = s.get("base_url", "").replace("https://", "").replace("http://", "").rstrip("/") or s["id"]

    return sites


def get_sites_cfg() -> List[Dict[str, Any]]:
    """便捷入口：读配置 + 规范化，返回站点列表。"""
    return normalize_sites(load_config_raw())


def make_new_site(base_url: str, system: str = "newapi", name: str = "") -> Dict[str, Any]:
    """创建一个新站点配置（配置页新增用）。"""
    import copy
    return {
        "id": db.normalize_site_id(base_url),
        "name": name or base_url.replace("https://", "").replace("http://", "").rstrip("/"),
        "system": system,
        "base_url": base_url,
        # New API 凭证
        "access_token": "",
        "session_cookie": "",
        # QAPI 凭证
        "email": "",
        "password": "",
        "api_key": "",
        "monitor": copy.deepcopy(DEFAULT_MONITOR),
        "notify": copy.deepcopy(DEFAULT_NOTIFY),
    }
