# -*- coding: utf-8 -*-
"""
Web-Session 池
管理多个小红书 web_session，支持轮换、冷却、健康检查。
"""
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from tools import utils

_POOL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cookies", "session_pool.json")
_lock = threading.Lock()


class SessionEntry:
    __slots__ = (
        "id", "web_session", "label", "status",
        "added_at", "last_used", "last_verified",
        "fail_count", "success_count", "cooldown_until",
    )

    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.web_session: str = data.get("web_session", "")
        self.label: str = data.get("label", "")
        self.status: str = data.get("status", "active")
        self.added_at: str = data.get("added_at", "")
        self.last_used: str = data.get("last_used", "")
        self.last_verified: str = data.get("last_verified", "")
        self.fail_count: int = data.get("fail_count", 0)
        self.success_count: int = data.get("success_count", 0)
        self.cooldown_until: Optional[str] = data.get("cooldown_until")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    def is_available(self) -> bool:
        if self.status == "expired":
            return False
        if self.status == "cooldown":
            if self.cooldown_until:
                try:
                    if datetime.fromisoformat(self.cooldown_until) <= datetime.now():
                        self.status = "active"
                        self.fail_count = 0
                        self.cooldown_until = None
                        return True
                except Exception:
                    pass
            return False
        return self.status == "active"


def _load_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "anti_crawl_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("session_pool", {})
    except Exception:
        return {}


class SessionPool:

    def __init__(self, pool_file: str = _POOL_FILE):
        self._file = pool_file
        self._entries: List[SessionEntry] = []
        self._index = 0
        self.load()

    # ── 持久化 ──

    def load(self):
        with _lock:
            if not os.path.exists(self._file):
                self._entries = []
                return
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._entries = [SessionEntry(d) for d in raw] if isinstance(raw, list) else []
            except Exception:
                self._entries = []

    def save(self):
        with _lock:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self._entries], f, ensure_ascii=False, indent=2)

    # ── 增删 ──

    def add(self, web_session: str, label: str = "") -> SessionEntry:
        web_session = web_session.strip()
        for e in self._entries:
            if e.web_session == web_session:
                raise ValueError("该 web_session 已存在")
        entry = SessionEntry({
            "id": f"s_{secrets.token_hex(6)}",
            "web_session": web_session,
            "label": label.strip(),
            "status": "active",
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "last_used": "",
            "last_verified": "",
            "fail_count": 0,
            "success_count": 0,
            "cooldown_until": None,
        })
        self._entries.append(entry)
        self.save()
        utils.logger.info(f"[SessionPool] 添加 session: {entry.id} ({label or web_session[:12]}...)")
        return entry

    def remove(self, session_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != session_id]
        if len(self._entries) < before:
            self.save()
            utils.logger.info(f"[SessionPool] 删除 session: {session_id}")
            return True
        return False

    # ── 查询 ──

    def get_all(self) -> List[dict]:
        for e in self._entries:
            e.is_available()
        return [e.to_dict() for e in self._entries]

    def get_by_id(self, session_id: str) -> Optional[SessionEntry]:
        for e in self._entries:
            if e.id == session_id:
                return e
        return None

    @property
    def active_count(self) -> int:
        return sum(1 for e in self._entries if e.is_available())

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def stats(self) -> dict:
        active = cooldown = expired = 0
        for e in self._entries:
            e.is_available()
            if e.status == "active":
                active += 1
            elif e.status == "cooldown":
                cooldown += 1
            elif e.status == "expired":
                expired += 1
        return {"total": len(self._entries), "active": active, "cooldown": cooldown, "expired": expired}

    # ── 轮换 ──

    def get_next(self) -> Optional[SessionEntry]:
        """
        取下一个可用 session（最久未使用优先）。
        返回 None 表示池为空。
        """
        available = [e for e in self._entries if e.is_available()]
        if not available:
            return None
        available.sort(key=lambda e: e.last_used or "")
        chosen = available[0]
        chosen.last_used = datetime.now().isoformat(timespec="seconds")
        self.save()
        return chosen

    # ── 状态标记 ──

    def mark_success(self, session_id: str):
        entry = self.get_by_id(session_id)
        if not entry:
            return
        entry.fail_count = 0
        entry.success_count += 1
        entry.last_used = datetime.now().isoformat(timespec="seconds")
        if entry.status == "cooldown":
            entry.status = "active"
            entry.cooldown_until = None
        self.save()

    def mark_failed(self, session_id: str):
        cfg = _load_config()
        max_fail = cfg.get("max_fail_count", 3)
        cooldown_min = cfg.get("cooldown_minutes", 30)

        entry = self.get_by_id(session_id)
        if not entry:
            return
        entry.fail_count += 1
        if entry.fail_count >= max_fail:
            entry.status = "cooldown"
            entry.cooldown_until = (datetime.now() + timedelta(minutes=cooldown_min)).isoformat(timespec="seconds")
            utils.logger.warning(
                f"[SessionPool] session {entry.id} 连续失败 {entry.fail_count} 次，"
                f"冷却 {cooldown_min} 分钟"
            )
        self.save()

    def mark_expired(self, session_id: str):
        entry = self.get_by_id(session_id)
        if not entry:
            return
        entry.status = "expired"
        entry.last_verified = datetime.now().isoformat(timespec="seconds")
        self.save()
        utils.logger.warning(f"[SessionPool] session {entry.id} 标记为已过期")

    def mark_active(self, session_id: str):
        entry = self.get_by_id(session_id)
        if not entry:
            return
        entry.status = "active"
        entry.fail_count = 0
        entry.cooldown_until = None
        entry.last_verified = datetime.now().isoformat(timespec="seconds")
        self.save()


def get_pool() -> SessionPool:
    """获取全局单例"""
    return SessionPool()
