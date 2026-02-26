# -*- coding: utf-8 -*-
"""
Web Terminal — 实时日志查看器
通过 WebSocket 流式推送日志文件内容（tail -f 模式）
"""
import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/terminal", tags=["terminal"])

PROJECT_ROOT = Path(__file__).parent.parent.parent

# 允许查看的日志文件白名单（相对于 PROJECT_ROOT）
ALLOWED_LOG_FILES = {
    "crawler.log": "logs/crawler.log",
    "session_keeper.log": "logs/session_keeper.log",
    "api.log": "api.log",
}

TAIL_INITIAL_LINES = 300
POLL_INTERVAL = 0.3


def _resolve_log_path(name: str) -> Path | None:
    rel = ALLOWED_LOG_FILES.get(name)
    if not rel:
        return None
    p = (PROJECT_ROOT / rel).resolve()
    if not str(p).startswith(str(PROJECT_ROOT.resolve())):
        return None
    return p


@router.get("/files")
async def list_log_files():
    """列出可查看的日志文件及其大小"""
    result = []
    for name, rel in ALLOWED_LOG_FILES.items():
        p = PROJECT_ROOT / rel
        exists = p.exists()
        size_kb = round(p.stat().st_size / 1024, 1) if exists else 0
        result.append({"name": name, "path": rel, "exists": exists, "size_kb": size_kb})
    return {"success": True, "data": result}


async def _tail_file(ws: WebSocket, filepath: Path):
    """
    读取文件最后 N 行，然后持续监听新内容并推送。
    文件轮转时自动重新打开。
    """
    if not filepath.exists():
        await ws.send_text(f"\x1b[33m[terminal] 日志文件不存在: {filepath.name}，等待创建...\x1b[0m\r\n")
        while not filepath.exists():
            await asyncio.sleep(1)
        await ws.send_text(f"\x1b[32m[terminal] 文件已创建，开始读取\x1b[0m\r\n")

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        # 读取最后 N 行
        lines = f.readlines()
        tail = lines[-TAIL_INITIAL_LINES:] if len(lines) > TAIL_INITIAL_LINES else lines
        header = f"\x1b[36m[terminal] === {filepath.name} (最后 {len(tail)} 行 / 共 {len(lines)} 行) ===\x1b[0m\r\n"
        await ws.send_text(header)

        for line in tail:
            await ws.send_text(_colorize(line.rstrip("\n")) + "\r\n")

        await ws.send_text(f"\x1b[36m[terminal] === 实时跟踪中 ===\x1b[0m\r\n")

        # 持续监听
        prev_inode = filepath.stat().st_ino
        while True:
            line = f.readline()
            if line:
                await ws.send_text(_colorize(line.rstrip("\n")) + "\r\n")
            else:
                await asyncio.sleep(POLL_INTERVAL)
                # 检测文件轮转（inode 变了 = 日志 rotate）
                try:
                    cur_inode = filepath.stat().st_ino
                    if cur_inode != prev_inode:
                        await ws.send_text(f"\x1b[33m[terminal] 日志文件已轮转，重新打开\x1b[0m\r\n")
                        return  # 返回后外层循环会重新打开文件
                except FileNotFoundError:
                    await ws.send_text(f"\x1b[33m[terminal] 文件已删除，等待重建...\x1b[0m\r\n")
                    return


def _colorize(line: str) -> str:
    """给日志行添加 ANSI 颜色"""
    if "ERROR" in line:
        return f"\x1b[31m{line}\x1b[0m"
    if "WARNING" in line:
        return f"\x1b[33m{line}\x1b[0m"
    if "✓" in line or "成功" in line or "完成" in line:
        return f"\x1b[32m{line}\x1b[0m"
    if "INFO" in line:
        # dim the timestamp portion, keep message normal
        return line
    return line


@router.websocket("/ws")
async def ws_terminal(ws: WebSocket):
    """
    WebSocket 终端端点。
    - 连接后立即开始 tail 默认日志文件
    - 客户端可发送 JSON {"type":"switch","file":"session_keeper.log"} 切换文件
    """
    await ws.accept()

    current_file = "crawler.log"
    cancel_event = asyncio.Event()

    async def run_tail(filename: str):
        path = _resolve_log_path(filename)
        if not path:
            await ws.send_text(f"\x1b[31m[terminal] 不允许查看的文件: {filename}\x1b[0m\r\n")
            return
        while not cancel_event.is_set():
            try:
                await _tail_file(ws, path)
            except (WebSocketDisconnect, Exception):
                return
            # _tail_file 返回 = 文件轮转，重新打开
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.5)

    tail_task = asyncio.create_task(run_tail(current_file))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                import json
                msg = json.loads(raw)
                if msg.get("type") == "switch":
                    new_file = msg.get("file", "")
                    if new_file and new_file != current_file and new_file in ALLOWED_LOG_FILES:
                        cancel_event.set()
                        tail_task.cancel()
                        try:
                            await tail_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        cancel_event = asyncio.Event()
                        current_file = new_file
                        await ws.send_text("\x1b[2J\x1b[H")  # clear screen
                        tail_task = asyncio.create_task(run_tail(current_file))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        cancel_event.set()
        tail_task.cancel()
        try:
            await tail_task
        except (asyncio.CancelledError, Exception):
            pass
