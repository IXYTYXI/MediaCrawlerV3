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
    r"进度|状态|情况|刷新|多少了|跑到哪|几个了|crawl|progress|status|refresh"
)

_dedup_lock = threading.Lock()
_processed_events: dict = {}
_DEDUP_TTL = 300
_DEDUP_MAX_SIZE = 1000

# 记住最近发送的进度卡片 message_id，用于原地更新
_last_card_lock = threading.Lock()
_last_card_msg_id: Optional[str] = None
_last_card_time: float = 0


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


def _reply_message(message_id: str, card: dict, token: str) -> Optional[str]:
    """回复飞书消息，返回发出的消息 message_id"""
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
            resp_data = resp.json()
            if resp_data.get("code") != 0:
                print(f"[feishu_reply] FAILED: {resp_data}", flush=True)
                return None
            sent_msg_id = resp_data.get("data", {}).get("message_id", "")
            print(f"[feishu_reply] SUCCESS, sent_msg_id={sent_msg_id}", flush=True)
            return sent_msg_id
    except Exception as e:
        print(f"[feishu_reply] EXCEPTION: {e}", flush=True)
        return None


def _patch_card(card_msg_id: str, card: dict, token: str) -> bool:
    """PATCH 更新已发送的卡片消息，实现原地刷新"""
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{card_msg_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
            )
            resp_data = resp.json()
            if resp_data.get("code") != 0:
                print(f"[feishu_patch] FAILED: {resp_data}", flush=True)
                return False
            print(f"[feishu_patch] SUCCESS, card updated in place", flush=True)
            return True
    except Exception as e:
        print(f"[feishu_patch] EXCEPTION: {e}", flush=True)
        return False


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

    print(f"[feishu_handler] msg_id={msg_id}, chat_type={chat_type}, msg_type={msg_type}")

    if not msg_id:
        print("[feishu_handler] no msg_id, skip")
        return

    if _is_duplicate(msg_id):
        print(f"[feishu_handler] duplicate msg_id={msg_id}, skip")
        return

    if chat_type == "group" and not message.get("mentions"):
        print("[feishu_handler] group msg without mention, skip")
        return

    if msg_type != "text":
        print(f"[feishu_handler] msg_type={msg_type}, not text, skip")
        return

    content_str = message.get("content", "{}")
    try:
        content = json.loads(content_str)
        text = content.get("text", "")
    except Exception:
        text = content_str

    text_clean = re.sub(r"@\S+", "", text).strip()
    print(f"[feishu_handler] text_clean='{text_clean}', has_match={bool(_PROGRESS_KEYWORDS.search(text_clean)) if text_clean else 'no-text'}")

    if text_clean and _PROGRESS_KEYWORDS.search(text_clean):
        try:
            _do_reply(msg_id)
        except Exception as e:
            import traceback
            print(f"[feishu_handler] _do_reply EXCEPTION: {e}\n{traceback.format_exc()}")
    else:
        print(f"[feishu_handler] no matching command, skip. text='{text_clean}'")


def _do_reply(message_id: str):
    """构建进度卡片：优先 PATCH 更新已有卡片，否则发新卡片"""
    global _last_card_msg_id, _last_card_time
    from tools.crawler_progress import parse_progress, build_progress_card

    print(f"[feishu_reply] building card for msg_id={message_id}", flush=True)
    creds = _load_feishu_credentials()
    token = _get_tenant_token(creds.get("app_id", ""), creds.get("app_secret", ""))
    if not token:
        print("[feishu_reply] FAILED to get tenant_token", flush=True)
        return

    progress = parse_progress()
    card = build_progress_card(progress)
    print(f"[feishu_reply] card built, size={len(json.dumps(card))}", flush=True)

    # 优先尝试 PATCH 更新已有卡片（30 分钟内的卡片才复用）
    with _last_card_lock:
        existing_msg_id = _last_card_msg_id
        existing_time = _last_card_time

    if existing_msg_id and (time.time() - existing_time < 1800):
        print(f"[feishu_reply] trying PATCH existing card {existing_msg_id}", flush=True)
        if _patch_card(existing_msg_id, card, token):
            with _last_card_lock:
                _last_card_time = time.time()
            return

    # PATCH 失败或没有已有卡片 → 发新卡片
    print(f"[feishu_reply] sending new card as reply", flush=True)
    sent_id = _reply_message(message_id, card, token)
    if sent_id:
        with _last_card_lock:
            _last_card_msg_id = sent_id
            _last_card_time = time.time()
        print(f"[feishu_reply] saved card msg_id={sent_id} for future PATCH", flush=True)


@router.post("/feishu/card_action")
async def feishu_card_action(request: Request):
    """
    飞书卡片交互回调端点（兼容 v1 旧格式 和 v2.0 新格式）

    v2.0 格式: action 在 body["event"]["action"]["value"] 中
    v1 旧格式: action 在 body["action"]["value"] 中

    飞书开放平台配置：应用 → 卡片回调 → 请求网址:
    https://<域名>/crawler/api/feishu/card_action
    """
    try:
        body = await request.json()
    except Exception:
        print("[card_action] invalid json body", flush=True)
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    print(f"[card_action] body keys: {list(body.keys())}", flush=True)

    # URL 验证（飞书首次配置回调地址时发送 challenge）
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        print(f"[card_action] url_verification OK", flush=True)
        return JSONResponse(content={"challenge": challenge})

    # 兼容 v2.0 和 v1 两种格式提取 action_value
    schema = body.get("schema", "")
    if schema == "2.0":
        # v2.0: body → header.token, event.action.value
        header = body.get("header", {})
        event = body.get("event", {})
        token_in_body = header.get("token", "")
        action = event.get("action", {})
        action_value = action.get("value", {})
        print(f"[card_action] v2.0 format, event_type={header.get('event_type')}, action_value={action_value}", flush=True)
    else:
        # v1 旧格式: body → token, action.value
        token_in_body = body.get("token", "")
        action = body.get("action", {})
        action_value = action.get("value", {})
        print(f"[card_action] v1 format, action_value={action_value}", flush=True)

    # 验证 token
    creds = _load_feishu_credentials()
    expected_token = creds.get("verification_token", "")
    if expected_token and token_in_body != expected_token:
        print(f"[card_action] token mismatch", flush=True)
        return JSONResponse(status_code=403, content={"error": "token mismatch"})

    if action_value.get("action") == "refresh_progress":
        from tools.crawler_progress import parse_progress, build_progress_card
        progress = parse_progress()
        card = build_progress_card(progress)

        # 同时尝试 PATCH 更新（作为双保险）
        open_message_id = body.get("open_message_id") or ""
        if open_message_id:
            creds_for_patch = _load_feishu_credentials()
            patch_token = _get_tenant_token(
                creds_for_patch.get("app_id", ""),
                creds_for_patch.get("app_secret", ""),
            )
            if patch_token:
                _patch_card(open_message_id, card, patch_token)

        resp_data = {
            "toast": {"type": "success", "content": "已刷新"},
            "card": {
                "type": "raw",
                "data": card,
            },
        }
        print(f"[card_action] returning card, size={len(json.dumps(resp_data))}", flush=True)
        return JSONResponse(content=resp_data)

    print(f"[card_action] no matching action, returning empty", flush=True)
    return JSONResponse(content={})


@router.post("/feishu/event")
async def feishu_event_callback(request: Request):
    """飞书事件订阅回调端点"""
    try:
        body = await request.json()
    except Exception:
        print("[feishu_event] invalid json body")
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    print(f"[feishu_event] received body keys: {list(body.keys())}")

    if body.get("type") == "url_verification":
        creds = _load_feishu_credentials()
        expected_token = creds.get("verification_token", "")
        if expected_token and body.get("token") != expected_token:
            return JSONResponse(status_code=403, content={"error": "token mismatch"})
        challenge = body.get("challenge", "")
        print(f"[feishu_event] url_verification, challenge={challenge[:20]}...")
        return JSONResponse(content={"challenge": challenge})

    schema = body.get("schema")
    header = body.get("header", {})
    event = body.get("event", {})

    creds = _load_feishu_credentials()
    expected_token = creds.get("verification_token", "")
    if expected_token and header.get("token") != expected_token:
        print(f"[feishu_event] token mismatch")
        return JSONResponse(status_code=403, content={"error": "token mismatch"})

    event_type = header.get("event_type", "")
    print(f"[feishu_event] schema={schema}, event_type={event_type}")

    if schema == "2.0" and event_type == "im.message.receive_v1":
        message = event.get("message", {})
        print(f"[feishu_event] message_id={message.get('message_id')}, chat_type={message.get('chat_type')}, content={message.get('content', '')[:100]}")
        threading.Thread(
            target=_handle_message_event,
            args=(event,),
            daemon=True,
        ).start()
        return JSONResponse(content={"code": 0})

    print(f"[feishu_event] unhandled event type: {event_type}")
    return JSONResponse(content={"code": 0})
