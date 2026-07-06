"""
多渠道通知模块。
支持：Server 酱 / Telegram / 企业微信群机器人 / 钉钉群机器人 / 邮件。
所有渠道都设计为「失败不抛异常、返回 (ok, msg)」，避免一个渠道挂掉影响其它。
"""
from __future__ import annotations

import logging
import smtplib
import time
import hmac
import hashlib
import base64
import urllib.parse
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List, Tuple

import requests

log = logging.getLogger(__name__)


def send_all(notify_cfg: Dict[str, Any], title: str, content: str) -> List[Tuple[str, bool, str]]:
    """
    依次调用所有 enabled 的渠道，返回 [(渠道名, 是否成功, 信息/错误), ...]
    title: 纯文本标题
    content: 支持 markdown 的正文（邮件/Server酱/markdown 渠道会用到）
    """
    results: List[Tuple[str, bool, str]] = []
    text_body = _strip_markdown(content)  # 纯文本兜底

    sc = notify_cfg.get("serverchan", {})
    if sc.get("enabled"):
        results.append(("serverchan", *_send_serverchan(sc, title, content)))

    tg = notify_cfg.get("telegram", {})
    if tg.get("enabled"):
        results.append(("telegram", *_send_telegram(tg, title, content)))

    wecom = notify_cfg.get("wecom", {})
    if wecom.get("enabled"):
        results.append(("wecom", *_send_wecom(wecom, title, text_body)))

    dt = notify_cfg.get("dingtalk", {})
    if dt.get("enabled"):
        results.append(("dingtalk", *_send_dingtalk(dt, title, text_body)))

    mail = notify_cfg.get("email", {})
    if mail.get("enabled"):
        results.append(("email", *_send_email(mail, title, text_body)))

    return results


# ----------------- 工具 -----------------
def _strip_markdown(md: str) -> str:
    """非常轻量的 markdown -> 纯文本，仅去掉常见符号，避免引入额外依赖。"""
    out = md
    for sym in ["**", "`", "#"]:
        out = out.replace(sym, "")
    return out


# ----------------- Server 酱 -----------------
def _send_serverchan(cfg: Dict[str, Any], title: str, desp: str) -> Tuple[bool, str]:
    key = (cfg.get("sendkey") or "").strip()
    if not key:
        return False, "sendkey 为空"
    url = f"https://sctapi.ftqq.com/{key}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": desp}, timeout=10)
        data = r.json()
        # serverchan 成功返回 code=0
        if r.status_code == 200 and data.get("code", -1) == 0:
            return True, "ok"
        return False, f"{r.status_code} {data}"
    except Exception as e:
        return False, repr(e)


# ----------------- Telegram -----------------
def _send_telegram(cfg: Dict[str, Any], title: str, content: str) -> Tuple[bool, str]:
    bot = (cfg.get("bot_token") or "").strip()
    chat = str(cfg.get("chat_id") or "").strip()
    if not bot or not chat:
        return False, "bot_token 或 chat_id 为空"
    url = f"https://api.telegram.org/bot{bot}/sendMessage"
    text = f"*{title}*\n\n{content}"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, f"{r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, repr(e)


# ----------------- 企业微信群机器人 -----------------
def _send_wecom(cfg: Dict[str, Any], title: str, text: str) -> Tuple[bool, str]:
    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        return False, "webhook 为空"
    payload = {
        "msgtype": "text",
        "text": {"content": f"【{title}】\n{text}"},
    }
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("errcode") == 0:
            return True, "ok"
        return False, f"{r.status_code} {data}"
    except Exception as e:
        return False, repr(e)


# ----------------- 钉钉群机器人 -----------------
def _send_dingtalk(cfg: Dict[str, Any], title: str, text: str) -> Tuple[bool, str]:
    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        return False, "webhook 为空"
    secret = (cfg.get("secret") or "").strip()

    final_url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        sign_str = f"{ts}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"), sign_str.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in final_url else "?"
        final_url = f"{final_url}{sep}timestamp={ts}&sign={sign}"

    payload = {
        "msgtype": "text",
        "text": {"content": f"【{title}】\n{text}"},
    }
    try:
        r = requests.post(final_url, json=payload, timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("errcode") == 0:
            return True, "ok"
        return False, f"{r.status_code} {data}"
    except Exception as e:
        return False, repr(e)


# ----------------- 邮件 -----------------
def _send_email(cfg: Dict[str, Any], title: str, text: str) -> Tuple[bool, str]:
    host = cfg.get("smtp_host")
    port = int(cfg.get("smtp_port", 465))
    use_ssl = bool(cfg.get("smtp_ssl", True))
    user = cfg.get("smtp_user")
    pwd = cfg.get("smtp_pass")
    from_addr = cfg.get("from_addr") or user
    to_addrs = [a for a in (cfg.get("to_addrs") or []) if a]
    if not (host and user and pwd and from_addr and to_addrs):
        return False, "SMTP 配置不完整"

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = formataddr(("倍率监测", from_addr))
    msg["To"] = ",".join(to_addrs)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, pwd)
                s.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                s.login(user, pwd)
                s.sendmail(from_addr, to_addrs, msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, repr(e)
