# -*- coding: utf-8 -*-
"""
飞书群消息通知
支持两种方式：
1. Webhook 自定义机器人（推荐，只需 webhook URL）
2. 应用机器人（需要 app_id/app_secret + chat_id）
"""
import json
import time
from typing import Optional, Dict, Any

import httpx

from tools import utils


def _build_summary_card(
    task_id: str,
    crawl_mode: str,
    total: int,
    success: int,
    failed: int,
    skipped: int,
    reused: int,
    elapsed_seconds: int,
    bitable_url: str = "",
    extra_info: str = "",
    at_user_ids: Optional[list] = None,
) -> Dict[str, Any]:
    """构建飞书消息卡片"""
    from datetime import datetime

    mode_labels = {
        "full": "全量爬取",
        "incremental": "增量更新",
        "date_range": "日期范围",
    }
    mode_emoji = {"full": "", "incremental": "", "date_range": ""}

    h = elapsed_seconds // 3600
    m = (elapsed_seconds % 3600) // 60
    s = elapsed_seconds % 60
    duration = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

    start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode_text = mode_labels.get(crawl_mode, crawl_mode)
    mode_icon = mode_emoji.get(crawl_mode, "🔄")

    all_ok = failed == 0
    header_color = "green" if all_ok else ("orange" if failed < total else "red")
    header_title = "爬取任务完成" if all_ok else "爬取任务完成（部分失败）"

    # @提醒
    at_text = ""
    if at_user_ids:
        at_parts = []
        for uid in at_user_ids:
            if uid == "all":
                at_parts.append("<at id=all></at>")
            else:
                at_parts.append(f"<at id={uid}></at>")
        at_text = " ".join(at_parts)

    # 进度条
    done = success + reused
    bar_len = 20
    filled = round(done / total * bar_len) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    pct = round(done / total * 100) if total > 0 else 0

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{start_time_str}　·　{mode_text}　·　{duration}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**完成进度**　`{bar}`　**{pct}%**（{done}/{total}）",
            },
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**{success}**\n成功"},
                    }],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**{reused}**\n复用"},
                    }],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**{failed}**\n失败"},
                    }],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**{skipped}**\n跳过"},
                    }],
                },
            ],
        },
    ]

    if bitable_url:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看飞书表格"},
                "type": "primary",
                "url": bitable_url,
            }],
        })

    if at_text:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": at_text},
        })

    if extra_info:
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": extra_info}],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_color,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


def send_webhook(
    webhook_url: str,
    task_id: str,
    crawl_mode: str = "full",
    total: int = 0,
    success: int = 0,
    failed: int = 0,
    skipped: int = 0,
    reused: int = 0,
    elapsed_seconds: int = 0,
    bitable_url: str = "",
    extra_info: str = "",
    secret: str = "",
    at_user_ids: Optional[list] = None,
) -> bool:
    """通过 Webhook 自定义机器人发送消息卡片"""
    if not webhook_url:
        return False

    card = _build_summary_card(
        task_id, crawl_mode, total, success, failed, skipped,
        reused, elapsed_seconds, bitable_url, extra_info, at_user_ids,
    )

    body: Dict[str, Any] = {
        "msg_type": "interactive",
        "card": card,
    }

    if secret:
        import hashlib
        import hmac
        import base64
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        body["timestamp"] = timestamp
        body["sign"] = sign

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(webhook_url, json=body)
            data = resp.json()
            if data.get("code") == 0 or data.get("StatusCode") == 0:
                utils.logger.info("[Notify] 飞书 Webhook 通知发送成功")
                return True
            else:
                utils.logger.warning(f"[Notify] Webhook 返回错误: {data}")
                return False
    except Exception as e:
        utils.logger.error(f"[Notify] Webhook 发送失败: {e}")
        return False


def send_app_bot(
    app_id: str,
    app_secret: str,
    chat_id: str,
    task_id: str,
    crawl_mode: str = "full",
    total: int = 0,
    success: int = 0,
    failed: int = 0,
    skipped: int = 0,
    reused: int = 0,
    elapsed_seconds: int = 0,
    bitable_url: str = "",
    extra_info: str = "",
    at_user_ids: Optional[list] = None,
) -> bool:
    """通过应用机器人发送消息卡片（需要 chat_id）"""
    if not app_id or not app_secret or not chat_id:
        return False

    try:
        with httpx.Client(timeout=15) as client:
            token_resp = client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            token_data = token_resp.json()
            token = token_data.get("tenant_access_token")
            if not token:
                utils.logger.error(f"[Notify] 获取 token 失败: {token_data}")
                return False

            card = _build_summary_card(
                task_id, crawl_mode, total, success, failed, skipped,
                reused, elapsed_seconds, bitable_url, extra_info, at_user_ids,
            )

            msg_resp = client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
            )
            msg_data = msg_resp.json()
            if msg_data.get("code") == 0:
                utils.logger.info("[Notify] 飞书应用机器人通知发送成功")
                return True
            else:
                utils.logger.warning(f"[Notify] 应用机器人返回错误: {msg_data}")
                return False
    except Exception as e:
        utils.logger.error(f"[Notify] 应用机器人发送失败: {e}")
        return False


def send_notification(
    task_id: str,
    crawl_mode: str = "full",
    total: int = 0,
    success: int = 0,
    failed: int = 0,
    skipped: int = 0,
    reused: int = 0,
    elapsed_seconds: int = 0,
    bitable_url: str = "",
    extra_info: str = "",
    notify_config: Optional[Dict] = None,
) -> bool:
    """
    统一通知入口，根据配置自动选择发送方式。
    notify_config 来自 anti_crawl_config.json 的 "notification" 字段。
    """
    if not notify_config or not notify_config.get("enabled", False):
        return False

    at_user_ids = notify_config.get("at_user_ids", [])

    kwargs = dict(
        task_id=task_id,
        crawl_mode=crawl_mode,
        total=total,
        success=success,
        failed=failed,
        skipped=skipped,
        reused=reused,
        elapsed_seconds=elapsed_seconds,
        bitable_url=bitable_url,
        extra_info=extra_info,
        at_user_ids=at_user_ids,
    )

    webhook_url = notify_config.get("webhook_url", "")
    if webhook_url:
        return send_webhook(
            webhook_url=webhook_url,
            secret=notify_config.get("webhook_secret", ""),
            **kwargs,
        )

    chat_id = notify_config.get("chat_id", "")
    app_id = notify_config.get("app_id", "")
    app_secret = notify_config.get("app_secret", "")

    # 未填 app_id/app_secret 时复用飞书推送的凭证
    if chat_id and (not app_id or not app_secret):
        try:
            import os
            _cfg_path = os.path.join("config", "anti_crawl_config.json")
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                import json as _json
                _feishu_cfg = _json.load(_f).get("feishu", {})
            app_id = app_id or _feishu_cfg.get("app_id", "")
            app_secret = app_secret or _feishu_cfg.get("app_secret", "")
        except Exception:
            pass

    if chat_id and app_id:
        return send_app_bot(
            app_id=app_id,
            app_secret=app_secret,
            chat_id=chat_id,
            **kwargs,
        )

    utils.logger.warning("[Notify] 通知已启用但未配置 webhook_url 或 chat_id，跳过")
    return False


def send_session_expired_alert(
    creator_name: str = "",
    creator_index: int = 0,
    total_creators: int = 0,
    login_url: str = "",
    notify_config: Optional[Dict] = None,
) -> bool:
    """Session 过期时发送紧急提醒"""
    if not notify_config or not notify_config.get("enabled", False):
        return False

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**当前进度**: {creator_index}/{total_creators}\n"
                    f"**当前作者**: {creator_name}\n\n"
                    "爬虫已暂停，等待重新登录（最多等待60分钟）。\n"
                    "**请尽快扫码恢复登录，否则任务将超时中止。**"
                ),
            },
        },
    ]

    if login_url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "前往登录"},
                "type": "danger",
                "url": login_url,
            }],
        })

    card = {
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "⚠️ Session 过期 - 需要重新登录"},
        },
        "elements": elements,
    }

    return _send_card(card, notify_config)


def _send_card(card: Dict, notify_config: Dict) -> bool:
    """发送任意卡片消息（内部复用）"""
    webhook_url = notify_config.get("webhook_url", "")
    if webhook_url:
        body: Dict[str, Any] = {"msg_type": "interactive", "card": card}
        secret = notify_config.get("webhook_secret", "")
        if secret:
            import hashlib, hmac, base64
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            body["timestamp"] = timestamp
            body["sign"] = base64.b64encode(hmac_code).decode("utf-8")
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(webhook_url, json=body)
                return resp.json().get("code", -1) == 0
        except Exception as e:
            utils.logger.error(f"[Notify] Webhook 发送失败: {e}")
            return False

    chat_id = notify_config.get("chat_id", "")
    app_id = notify_config.get("app_id", "")
    app_secret = notify_config.get("app_secret", "")
    if chat_id and (not app_id or not app_secret):
        try:
            import os
            _cfg_path = os.path.join("config", "anti_crawl_config.json")
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _feishu_cfg = json.load(_f).get("feishu", {})
            app_id = app_id or _feishu_cfg.get("app_id", "")
            app_secret = app_secret or _feishu_cfg.get("app_secret", "")
        except Exception:
            pass

    if chat_id and app_id:
        try:
            with httpx.Client(timeout=15) as client:
                token_resp = client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                token = token_resp.json().get("tenant_access_token")
                if not token:
                    return False
                msg_resp = client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "interactive",
                        "content": json.dumps(card),
                    },
                )
                return msg_resp.json().get("code", -1) == 0
        except Exception as e:
            utils.logger.error(f"[Notify] 应用机器人发送失败: {e}")
            return False

    return False
