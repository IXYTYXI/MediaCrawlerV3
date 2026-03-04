# -*- coding: utf-8 -*-
"""
飞书事件订阅回调路由
在群聊中 @机器人 询问"进度"，自动回复爬虫实时状态卡片。

飞书开放平台配置步骤：
1. 进入应用 → 事件订阅 → 设置请求网址: https://<域名>/crawler/api/feishu/event
2. 添加事件: im.message.receive_v1（接收消息）
3. 需要权限: im:message:send_as_bot, im:message:receive_v1
"""
import json
import logging
import os
import re
import threading
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["feishu_event"])
logger = logging.getLogger("MediaCrawler")

CONFIG_PATH = os.path.join("config", "anti_crawl_config.json")

_PROGRESS_KEYWORDS = re.compile(
    r"进度|状态|情况|多少了|跑到哪|几个了|crawl|progress|status"
)

_dedup_lock = threading.Lock()
_processed_events: dict = {}
_DEDUP_TTL = 300
_DEDUP_MAX_SIZE = 1000


def _load_feishu_credentials() -> dict:
    """从配置文件加载飞书凭证"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        feishu = cfg.get("feishu", {})
        notify = cfg.get("notification", {})
        app_id = notify.get("app_id") or feishu.get("app_id", "")
        app_secret = notify.get("app_secret") or feishu.get("app_secret", "")
        verification_token = notify.get("verification_token", "")
        return {
            "app_id": app_id,
            "app_secret": app_secret,
            "verification_token": verification_token,
        }
    except Exception:
        return {}


def _get_tenant_token(app_id: str, app_secret: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            return resp.json().get("tenant_access_token")
    except Exception:
        return None


def _reply_message(message_id: str, card: dict, token: str):
    """回复飞书消息（在后台线程执行，避免阻塞）"""
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
            )
            if resp.json().get("code") != 0:
                logger.warning(f"[FeishuEvent] 回复失败: {resp.json()}")
    except Exception as e:
        logger.warning(f"[FeishuEvent] 回复异常: {e}")


def _is_duplicate(msg_id: str) -> bool:
    """线程安全的消息去重"""
    now = time.time()
    with _dedup_lock:
        expired = [k for k, v in _processed_events.items() if now - v > _DEDUP_TTL]
        for k in expired:
            _processed_events.pop(k, None)
        if len(_processed_events) > _DEDUP_MAX_SIZE:
            oldest = sorted(_processed_events, key=_processed_events.get)
            for k in oldest[:len(oldest) // 2]:
                _processed_events.pop(k, None)
        if msg_id in _processed_events:
            return True
        _processed_events[msg_id] = now
        return False


def _handle_message_event(event: dict):
    """处理收到的消息事件"""
    message = event.get("message", {})
    msg_id = message.get("message_id", "")
    chat_type = message.get("chat_type", "")
    msg_type = message.get("message_type", "")

    if not msg_id:
        return

    if _is_duplicate(msg_id):
        return

    if chat_type == "group" and not message.get("mentions"):
        return

    if msg_type != "text":
        return

    content_str = message.get("content", "{}")
    try:
        content = json.loads(content_str)
        text = content.get("text", "")
    except Exception:
        text = content_str

    text_clean = re.sub(r"@\S+", "", text).strip()

    if text_clean and _PROGRESS_KEYWORDS.search(text_clean):
        _do_reply(msg_id)
    elif not text_clean:
        # @机器人 但没有其他文字 → 也回复进度
        _do_reply(msg_id)


def _do_reply(message_id: str):
    """构建进度卡片并回复"""
    from tools.crawler_progress import parse_progress, build_progress_card

    creds = _load_feishu_credentials()
    token = _get_tenant_token(creds.get("app_id", ""), creds.get("app_secret", ""))
    if not token:
        logger.warning("[FeishuEvent] 获取 tenant_token 失败，无法回复")
        return

    progress = parse_progress()
    card = build_progress_card(progress)
    _reply_message(message_id, card, token)


@router.post("/feishu/card_action")
async def feishu_card_action(request: Request):
    """
    飞书卡片交互回调端点
    当用户点击卡片中的按钮时，飞书会 POST 到此端点。
    返回新的卡片 JSON 即可原地更新卡片内容。

    飞书开放平台配置：应用 → 卡片回调 → 请求网址:
    https://<域名>/crawler/api/feishu/card_action
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    # URL 验证（飞书首次配置回调地址时发送 challenge）
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        return JSONResponse(content={"challenge": challenge})

    # 验证 token
    creds = _load_feishu_credentials()
    expected_token = creds.get("verification_token", "")
    if expected_token and body.get("token") != expected_token:
        return JSONResponse(status_code=403, content={"error": "token mismatch"})

    action = body.get("action", {})
    action_value = action.get("value", {})

    if action_value.get("action") == "refresh_progress":
        from tools.crawler_progress import parse_progress, build_progress_card
        progress = parse_progress()
        card = build_progress_card(progress)
        return JSONResponse(content={
            "toast": {"type": "success", "content": "已刷新"},
            "card": {
                "type": "raw",
                "data": card,
            },
        })

    return JSONResponse(content={})


@router.post("/feishu/event")
async def feishu_event_callback(request: Request):
    """飞书事件订阅回调端点"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    # URL 验证（首次配置时飞书会发送 challenge）
    if body.get("type") == "url_verification":
        creds = _load_feishu_credentials()
        expected_token = creds.get("verification_token", "")
        if expected_token and body.get("token") != expected_token:
            return JSONResponse(status_code=403, content={"error": "token mismatch"})
        challenge = body.get("challenge", "")
        return JSONResponse(content={"challenge": challenge})

    # V2 事件格式
    schema = body.get("schema")
    header = body.get("header", {})
    event = body.get("event", {})

    # 验证 verification_token（防伪造请求）
    creds = _load_feishu_credentials()
    expected_token = creds.get("verification_token", "")
    if expected_token and header.get("token") != expected_token:
        return JSONResponse(status_code=403, content={"error": "token mismatch"})

    if schema == "2.0" and header.get("event_type") == "im.message.receive_v1":
        threading.Thread(
            target=_handle_message_event,
            args=(event,),
            daemon=True,
        ).start()
        return JSONResponse(content={"code": 0})

    return JSONResponse(content={"code": 0})
