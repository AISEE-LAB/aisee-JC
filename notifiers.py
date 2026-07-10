"""
多渠道通知模块。
支持：Server 酱 / Telegram / 企业微信群机器人 / 钉钉群机器人 / 邮件 / Bark / Discord / 飞书。
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

    bark = notify_cfg.get("bark", {})
    if bark.get("enabled"):
        results.append(("bark", *_send_bark(bark, title, text_body)))

    discord = notify_cfg.get("discord", {})
    if discord.get("enabled"):
        results.append(("discord", *_send_discord(discord, title, content)))

    feishu = notify_cfg.get("feishu", {})
    if feishu.get("enabled"):
        results.append(("feishu", *_send_feishu(feishu, title, text_body)))

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


# ----------------- Bark（iOS 推送）-----------------
def _send_bark(cfg: Dict[str, Any], title: str, text: str) -> Tuple[bool, str]:
    """Bark 推送。配置：server（如 https://api.day.app 或自建地址）+ device_key。
    支持自定义铃声/图标，这里走 POST JSON 接口。
    """
    server = (cfg.get("server") or "https://api.day.app").strip().rstrip("/")
    key = (cfg.get("device_key") or "").strip()
    if not key:
        return False, "device_key 为空"
    url = f"{server}/{key}"
    payload = {
        "title": title,
        "body": text,
        "group": cfg.get("group") or "rate-monitor",
        "sound": cfg.get("sound") or "alert",
        "autoCopy": False,
    }
    icon = (cfg.get("icon") or "").strip()
    if icon:
        payload["icon"] = icon
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("code") in (200, "200", None):
            # Bark 返回 code=200 表示成功；某些版本无 code 字段，HTTP 200 即视为成功
            return True, "ok"
        if r.status_code == 200:
            return True, "ok"
        return False, f"{r.status_code} {data or r.text[:200]}"
    except Exception as e:
        return False, repr(e)


# ----------------- Discord Webhook -----------------
def _send_discord(cfg: Dict[str, Any], title: str, content: str) -> Tuple[bool, str]:
    """Discord 频道 Webhook。content 为 markdown 正文，Discord 原生支持 markdown。"""
    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        return False, "webhook 为空"
    # Discord 单条消息上限 2000 字符，超出截断
    body = f"**{title}**\n\n{content}"
    if len(body) > 1900:
        body = body[:1900] + "\n…(截断)"
    payload = {"content": body, "username": cfg.get("username") or "倍率监测"}
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        # Discord 成功返回 204 No Content（无响应体）
        if r.status_code in (200, 204):
            return True, "ok"
        return False, f"{r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, repr(e)


# ----------------- 飞书群机器人 -----------------
def _send_feishu(cfg: Dict[str, Any], title: str, text: str) -> Tuple[bool, str]:
    """飞书自定义机器人 Webhook。
    - 无签名：直接 POST webhook
    - 启用签名：用 secret 生成 sign，放入 payload
    """
    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        return False, "webhook 为空"
    secret = (cfg.get("secret") or "").strip()

    import time as _t
    payload: Dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": f"【{title}】\n{text}"},
    }
    if secret:
        # 飞书签名算法：timestamp + "\n" + secret -> HmacSHA256，再 base64
        ts = str(int(_t.time()))
        sign_str = f"{ts}\n{secret}"
        hmac_code = hmac.new(sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        # 飞书成功返回 {"code":0,"msg":"success"} 或 {"StatusCode":0}
        code = data.get("code", data.get("StatusCode", -1))
        if r.status_code == 200 and (code == 0 or code == "0"):
            return True, "ok"
        return False, f"{r.status_code} {data or r.text[:200]}"
    except Exception as e:
        return False, repr(e)
