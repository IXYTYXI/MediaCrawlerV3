# -*- coding: utf-8 -*-
"""
Shell 命令行 — 密码保护的 bash 终端
需通过控制面板密码认证，无 token 无法执行任何命令
运行 tail -f logs/crawler.log 可实时查看爬虫输出，Ctrl+C 中止
"""
import asyncio
import os
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/shell", tags=["shell"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 禁止执行的命令模式（正则）
_BLOCKED_PATTERNS = [
    r"\bsudo\b",
    r"\bsu\s",
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+\*\s+/",
    r":\s*\(\s*\)",
    r">\s*/dev/sda",
    r"mkfs\.\w+",
    r"dd\s+if=.*of=/dev/",
    r"chmod\s+-R\s+777\s+/",
    r"wget\s+.*\|\s*sh",
    r"curl\s+.*\|\s*sh",
    r"curl\s+.*\|\s*bash",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.I) if _BLOCKED_PATTERNS else None


def _is_command_blocked(cmd: str) -> bool:
    """检查命令是否在禁止列表中"""
    if not cmd or not cmd.strip():
        return True
    if _BLOCKED_RE and _BLOCKED_RE.search(cmd):
        return True
    return False


async def _run_command(cmd: str, ws: WebSocket, current_proc: list, stop_event: asyncio.Event) -> int:
    """在项目目录下执行命令，流式输出到 WebSocket；stop_event 置位时立即终止"""
    if _is_command_blocked(cmd):
        await ws.send_text("\x1b[31m[shell] 该命令已被禁止执行\x1b[0m\r\n")
        return -1
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=PROJECT_ROOT,
            env={**os.environ},
        )
        current_proc.clear()
        current_proc.append(proc)
        stop_event.clear()
        try:
            while True:
                read_task = asyncio.create_task(proc.stdout.readline())
                wait_task = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    [read_task, wait_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                if stop_event.is_set():
                    read_task.cancel()
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    break
                line = await read_task
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                try:
                    await ws.send_text(text)
                except Exception:
                    break
            return proc.returncode or 0
        finally:
            current_proc.clear()
    except asyncio.CancelledError:
        return -1
    except Exception as e:
        await ws.send_text(f"\x1b[31m[shell] 执行失败: {e}\x1b[0m\r\n")
        return -1


@router.websocket("/ws")
async def ws_shell(ws: WebSocket):
    """
    WebSocket 命令行端点。
    由 auth 中间件校验 token，无密码登录无法连接。
    发送文本即作为命令执行，输出流式返回。
    """
    await ws.accept()

    # 发送欢迎信息
    cwd = os.getcwd()
    try:
        cwd = PROJECT_ROOT
    except Exception:
        pass
    welcome = f"\x1b[36m[shell] MediaCrawler 命令行 (工作目录: {PROJECT_ROOT})\r\n"
    welcome += "[shell] 输入命令按回车执行，Ctrl+C 中止当前命令，exit 断开\r\n"
    welcome += "[shell] 查看爬虫实时日志: \x1b[33mtail -f logs/crawler.log\x1b[0m\r\n\x1b[0m\r\n"
    welcome += "\x1b[32m$ \x1b[0m"
    try:
        await ws.send_text(welcome)
    except Exception:
        return

    current_proc = []
    cmd_task = None
    stop_event = asyncio.Event()

    recv_task = asyncio.create_task(asyncio.wait_for(ws.receive_text(), timeout=3600))

    try:
        while True:
            tasks = [recv_task]
            if cmd_task and not cmd_task.done():
                tasks.append(cmd_task)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            # 收到新消息
            if recv_task in done:
                try:
                    raw = recv_task.result()
                except asyncio.TimeoutError:
                    try:
                        await ws.send_text("\r\n\x1b[33m[shell] 连接超时，即将断开\x1b[0m\r\n")
                    except Exception:
                        pass
                    break
                recv_task = asyncio.create_task(asyncio.wait_for(ws.receive_text(), timeout=3600))

                if raw == "\x03" or raw == "\x1b":
                    stop_event.set()
                    if current_proc:
                        try:
                            proc = current_proc[0]
                            proc.terminate()
                            await asyncio.wait_for(proc.wait(), timeout=2)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        current_proc.clear()
                    if cmd_task and not cmd_task.done():
                        cmd_task.cancel()
                        try:
                            await cmd_task
                        except asyncio.CancelledError:
                            pass
                    try:
                        await ws.send_text("\r\n\x1b[33m^C\x1b[0m\r\n\x1b[32m$ \x1b[0m")
                    except Exception:
                        pass
                    continue

                raw = raw.strip()
                if not raw:
                    continue
                if raw.lower() in ("exit", "quit", "logout"):
                    await ws.send_text("\x1b[36m[shell] 再见\x1b[0m\r\n")
                    break

                if cmd_task and not cmd_task.done():
                    cmd_task.cancel()
                    try:
                        await cmd_task
                    except asyncio.CancelledError:
                        pass
                cmd_task = asyncio.create_task(_run_command(raw, ws, current_proc, stop_event))

            # 命令执行完毕（非 Ctrl+C 取消时显示新提示）
            if cmd_task in done and not cmd_task.cancelled():
                cmd_task = None
                try:
                    await ws.send_text("\x1b[32m$ \x1b[0m")
                except Exception:
                    pass
            elif cmd_task in done:
                cmd_task = None

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
