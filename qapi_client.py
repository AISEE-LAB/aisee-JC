"""
QAPI (sub2api / Unity2) 抓取客户端。

与 New API 的差异：
- 后台接口 /api/v1/* 需要 JWT（用账号密码登录 /api/v1/auth/login 拿）
- 网关接口 /v1/models 用 API Key（sk-xxx）
- 没有公开的「模型+价格」接口；分组倍率在 /api/v1/groups/available 的 rate_multiplier

鉴权策略：
- email + password → 登录拿 JWT（缓存到内存，失效自动重登）
- api_key（sk-xxx）→ 用于 /v1/models

抓取三项数据：
1. 分组倍率：GET /api/v1/groups/available → rate_multiplier
2. 模型清单：GET /v1/models → 模型增减监测
3. 花费趋势：GET /api/v1/usage/dashboard/models → 实际花费（间接反映价格）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)


class QapiClient:
    def __init__(self, site_cfg: Dict[str, Any]):
        self.base = (site_cfg.get("base_url") or "").rstrip("/")
        self.email = (site_cfg.get("email") or "").strip()
        self.password = site_cfg.get("password") or ""
        self.api_key = (site_cfg.get("api_key") or "").strip()
        self.timeout = int(site_cfg.get("timeout", 15))
        self.retry = int(site_cfg.get("retry", 3))
        if not self.base:
            raise ValueError("site.base_url 未配置")
        self._jwt: Optional[str] = None  # JWT 缓存

    # ---------------- JWT 登录 ----------------
    def _login(self) -> str:
        """用 email/password 登录，返回 JWT。失败抛异常。"""
        if not (self.email and self.password):
            raise ValueError("QAPI 需要 email + password 登录拿 JWT")
        url = f"{self.base}/api/v1/auth/login"
        r = requests.post(
            url,
            json={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/json", "Accept-Language": "zh-CN"},
            timeout=self.timeout,
        )
        data = r.json() if r.status_code != 204 else {}
        # QAPI (sub2api) 真实结构：{"code":0,"message":"success","data":{"access_token":"...","refresh_token":"..."}}
        # 兼容多种结构：data.access_token / data.token / 顶层 token / access_token
        token = None
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            token = (
                inner.get("access_token")
                or inner.get("token")
                or inner.get("jwt")
                or inner.get("accessToken")
                or data.get("access_token")
                or data.get("token")
            )
        # 兜底：从 Authorization header 取
        if not token:
            token = r.headers.get("Authorization") or r.headers.get("token")
        if not token:
            # 登录失败：QAPI 用 code != 0 表示失败，message 是错误信息
            msg = data.get("message") if isinstance(data, dict) else f"HTTP {r.status_code}"
            raise RuntimeError(f"QAPI 登录失败：{msg}")
        # 规范化：去掉可能的 "Bearer " 前缀
        if token.lower().startswith("bearer "):
            token = token[7:]
        log.info("QAPI 登录成功（账号 %s）", self.email)
        return token

    def _get_jwt(self) -> str:
        if not self._jwt:
            self._jwt = self._login()
        return self._jwt

    def _invalidate_jwt(self) -> None:
        self._jwt = None

    # ---------------- 通用请求 ----------------
    def _get(self, path: str, auth: str = "jwt", params: Optional[Dict] = None) -> Tuple[int, Any]:
        """发起 GET 请求。
        auth: 'jwt' = 后台接口（带 JWT，401 自动重登一次）；'apikey' = 网关接口
        返回 (status_code, parsed_json_or_text)
        """
        url = f"{self.base}{path}"
        last_err = None
        for attempt in range(1, self.retry + 1):
            headers = {"Accept": "application/json", "Accept-Language": "zh-CN"}
            if auth == "jwt":
                headers["Authorization"] = f"Bearer {self._get_jwt()}"
            elif auth == "apikey":
                if not self.api_key:
                    return 401, {"code": "NO_API_KEY", "message": "api_key 未配置"}
                headers["Authorization"] = f"Bearer {self.api_key}"
            try:
                r = requests.get(url, headers=headers, params=params, timeout=self.timeout)
                # JWT 失效：401 → 重登一次
                if r.status_code == 401 and auth == "jwt" and attempt == 1:
                    log.info("JWT 失效，重新登录")
                    self._invalidate_jwt()
                    continue
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, r.text
            except requests.RequestException as e:
                last_err = repr(e)
                log.warning("GET %s 异常：%s (尝试 %s/%s)", path, last_err, attempt, self.retry)
                time.sleep(1.5 * attempt)
        log.error("GET %s 最终失败：%s", path, last_err)
        return 0, {"code": "REQUEST_FAILED", "message": last_err or "请求失败"}

    # ---------------- 业务接口 ----------------
    def fetch_user_profile(self) -> Optional[Dict[str, Any]]:
        code, data = self._get("/api/v1/user/profile", auth="jwt")
        if code != 200 or not isinstance(data, dict):
            log.warning("user/profile 失败：%s %s", code, _short(data))
            return None
        return data.get("data") or data

    def fetch_groups_available(self) -> List[Dict[str, Any]]:
        """返回用户可用分组列表（含 rate_multiplier 分组倍率）。
        管理员账号会优先用 /api/v1/admin/groups 拿全量分组，否则用用户视角。
        """
        # 管理员优先：admin 接口能拿到所有分组（含未启用的）
        code, data = self._get("/api/v1/admin/groups", auth="jwt", params={"page_size": 200})
        if code == 200 and isinstance(data, dict):
            d = data.get("data")
            items = d.get("items") if isinstance(d, dict) else d
            if isinstance(items, list) and items:
                log.info("通过 admin 接口拿到 %d 个分组", len(items))
                return items
        # 退回用户视角
        code, data = self._get("/api/v1/groups/available", auth="jwt")
        if code != 200 or not isinstance(data, dict):
            log.warning("groups/available 失败：%s %s", code, _short(data))
            return []
        d = data.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict) and isinstance(d.get("groups"), list):
            return d["groups"]
        return []

    def fetch_models(self) -> List[str]:
        """通过 API Key 调用 /v1/models，返回模型名列表（不含价格）。"""
        code, data = self._get("/v1/models", auth="apikey")
        if code != 200 or not isinstance(data, dict):
            log.warning("v1/models 失败：%s %s（如果没配 api_key，模型增减监测将跳过）", code, _short(data))
            return []
        out = []
        for m in data.get("data") or []:
            name = m.get("id") or m.get("name")
            if name:
                out.append(name)
        return out

    def fetch_usage_models(self, days: int = 30) -> List[Dict[str, Any]]:
        """已用模型的实际花费（间接反映价格变化）。
        返回 [{model, requests, cost, actual_cost, ...}]
        """
        params = {"days": days}
        code, data = self._get("/api/v1/usage/dashboard/models", auth="jwt", params=params)
        if code != 200 or not isinstance(data, dict):
            log.warning("usage/dashboard/models 失败：%s %s", code, _short(data))
            return []
        d = data.get("data")
        if isinstance(d, dict):
            return d.get("models") or []
        if isinstance(d, list):
            return d
        return []


def _short(v: Any, n: int = 120) -> str:
    try:
        s = str(v)
        return s[:n] + ("…" if len(s) > n else "")
    except Exception:
        return str(type(v))
