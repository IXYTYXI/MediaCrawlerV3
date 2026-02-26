# -*- coding: utf-8 -*-
"""
Dashboard API - 配置管理 + 批量爬取控制 + Session 管理
提供前端 Dashboard 所需的所有接口
"""
import asyncio
import json
import os
import re
import subprocess
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "anti_crawl_config.json"
BATCH_PROGRESS_PATH = PROJECT_ROOT / "data" / "batch_progress.json"
EXCEL_PATH_DEFAULT = PROJECT_ROOT / "redbookaccontidandresult.xlsx"

# 批量爬取进程管理
_batch_process: Optional[subprocess.Popen] = None
_batch_status = {"status": "idle", "started_at": None, "message": ""}
_batch_logs = []
_ws_clients: set = set()


# ==================== 配置管理 ====================

@router.get("/config")
async def get_config():
    """获取完整配置"""
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="配置文件不存在")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return {"success": True, "data": config}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")


@router.put("/config")
async def update_config(config: dict):
    """更新配置（整体替换）"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return {"success": True, "message": "配置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {e}")


@router.patch("/config/{section}")
async def update_config_section(section: str, data: dict):
    """更新配置的某个部分"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

        if section not in config:
            raise HTTPException(status_code=404, detail=f"配置项 '{section}' 不存在")

        # 合并更新
        if isinstance(config[section], dict):
            config[section].update(data)
        else:
            config[section] = data

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        return {"success": True, "message": f"配置 '{section}' 已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新配置失败: {e}")


# ==================== Excel 作者列表 ====================

@router.get("/creators")
async def get_creators():
    """获取 Excel 中的作者列表"""
    try:
        # 从配置读取 Excel 路径
        excel_path = EXCEL_PATH_DEFAULT
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            custom_path = cfg.get("batch_crawl", {}).get("excel_path", "")
            if custom_path and os.path.exists(custom_path):
                excel_path = Path(custom_path)

        if not excel_path.exists():
            return {"success": True, "data": [], "message": "Excel 文件不存在"}

        from tools.excel_reader import ExcelCreatorReader
        with ExcelCreatorReader(str(excel_path)) as reader:
            creators = reader.get_creators()
            fields = reader.get_result_fields()

        return {"success": True, "data": {"creators": creators, "fields": fields}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取作者列表失败: {e}")


# ==================== 批量爬取控制 ====================

class BatchStartRequest(BaseModel):
    limit: int = 0
    min_interaction: int = 0
    skip_feishu: bool = True
    enable_comments: bool = False
    max_notes: int = 3000
    force_recrawl: bool = False


@router.post("/batch/start")
async def start_batch_crawl(request: BatchStartRequest):
    """启动批量爬取"""
    global _batch_process, _batch_status, _batch_logs

    if _batch_process and _batch_process.poll() is None:
        raise HTTPException(status_code=400, detail="批量爬取正在运行中")

    _batch_logs = []

    # 构建命令
    cmd = ["conda", "run", "--no-capture-output", "-n", "uvenv", "python", "-u", "-m", "tools.batch_crawler"]
    if request.skip_feishu:
        cmd.append("--skip-feishu")
    if request.limit > 0:
        cmd.extend(["--limit", str(request.limit)])
    cmd.extend(["--min-interaction", str(request.min_interaction)])
    cmd.extend(["--max-notes", str(request.max_notes)])
    if request.enable_comments:
        cmd.append("--enable-comments")
    if request.force_recrawl:
        cmd.append("--force-recrawl")

    try:
        _batch_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        _batch_status = {
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "message": f"批量爬取已启动 (limit={request.limit})",
        }

        # 后台读日志
        asyncio.create_task(_read_batch_output())

        return {"success": True, "message": "批量爬取已启动", "pid": _batch_process.pid}
    except Exception as e:
        _batch_status = {"status": "error", "started_at": None, "message": str(e)}
        raise HTTPException(status_code=500, detail=f"启动失败: {e}")


@router.post("/batch/stop")
async def stop_batch_crawl():
    """优雅停止批量爬取（发送 SIGTERM，等待数据保存后退出）"""
    global _batch_process, _batch_status

    if not _batch_process or _batch_process.poll() is not None:
        raise HTTPException(status_code=400, detail="没有正在运行的批量爬取")

    pid = _batch_process.pid
    try:
        _batch_status["message"] = "正在保存当前进度..."
        _batch_process.send_signal(signal.SIGTERM)

        # 等待优雅退出（最多 30 秒，足够保存数据）
        graceful = False
        for _ in range(60):
            if _batch_process.poll() is not None:
                graceful = True
                break
            await asyncio.sleep(0.5)

        if not graceful and _batch_process.poll() is None:
            _batch_process.kill()
            _batch_process.wait()

        _batch_status = {"status": "idle", "started_at": None, "message": "已停止"}

        # 读取最新进度
        summary = _get_task_summary()
        msg = (
            f"已优雅停止 (pid={pid})"
            if graceful
            else f"已强制停止 (pid={pid})"
        )
        return {
            "success": True,
            "message": msg,
            "graceful": graceful,
            "task_summary": summary,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"停止失败: {e}")


@router.get("/batch/status")
async def get_batch_status():
    """获取批量爬取状态"""
    global _batch_process, _batch_status

    # 检查进程是否已结束
    if _batch_process and _batch_process.poll() is not None:
        if _batch_status["status"] == "running":
            _batch_status["status"] = "completed"
            _batch_status["message"] = f"已完成 (exit code: {_batch_process.returncode})"

    return {
        "success": True,
        "data": _batch_status,
        "log_count": len(_batch_logs),
    }


CRAWLER_LOG_PATH = PROJECT_ROOT / "logs" / "crawler.log"


def _read_crawler_log_tail(n: int = 500) -> list:
    """从 crawler.log 读取最后 n 行，转为 Dashboard 日志格式（控制面板/外部启动时用）"""
    if not CRAWLER_LOG_PATH.exists():
        return []
    try:
        with open(CRAWLER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 512 * 1024)
            f.seek(max(0, size - chunk))
            raw = f.read()
        lines = [ln.rstrip("\n\r") for ln in raw.splitlines() if ln.strip()][-n:]
        result = []
        for line in lines:
            time_part = "--"
            msg_part = line
            if len(line) >= 19 and line[4] == "-" and line[7] == "-" and line[10] == " " and line[13] == ":":
                time_part = line[11:19]
                msg_part = line[20:] if len(line) > 20 else line
            result.append({"time": time_part, "message": msg_part})
        return result
    except Exception:
        return []


@router.get("/batch/logs")
async def get_batch_logs(limit: int = 500, offset: int = 0):
    """获取批量爬取日志。Dashboard 启动时用 _batch_logs；否则从 crawler.log 读取（控制面板/外部启动）"""
    if _batch_logs:
        logs = _batch_logs[offset:offset + limit] if limit > 0 else _batch_logs[offset:]
        return {"success": True, "data": logs, "total": len(_batch_logs)}
    fallback = _read_crawler_log_tail(limit)
    return {"success": True, "data": fallback, "total": len(fallback)}


async def _read_batch_output():
    """后台读取批量爬取输出，同时推送到 WebSocket 客户端"""
    global _batch_process, _batch_status, _batch_logs
    loop = asyncio.get_event_loop()

    async def _append_and_broadcast(line_text):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": line_text}
        _batch_logs.append(entry)
        dead = set()
        for ws in _ws_clients:
            try:
                await ws.send_json({"type": "log", **entry})
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)

    try:
        while _batch_process and _batch_process.poll() is None:
            line = await loop.run_in_executor(None, _batch_process.stdout.readline)
            if line and line.strip():
                await _append_and_broadcast(line.strip())
        if _batch_process and _batch_process.stdout:
            remaining = await loop.run_in_executor(None, _batch_process.stdout.read)
            if remaining:
                for line in remaining.strip().split("\n"):
                    if line.strip():
                        await _append_and_broadcast(line.strip())
        # 通知所有客户端爬取结束
        for ws in _ws_clients:
            try:
                await ws.send_json({"type": "status", "status": "completed"})
            except Exception:
                pass
    except Exception:
        pass


@router.websocket("/batch/logs/ws")
async def batch_logs_ws(ws: WebSocket):
    """WebSocket 实时日志流"""
    await ws.accept()
    _ws_clients.add(ws)
    try:
        # 先发送已有日志
        for entry in _batch_logs[-200:]:
            await ws.send_json({"type": "log", **entry})
        # 保持连接
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(ws)


# ==================== 任务摘要 + 批量进度 ====================


def _resolve_progress_path() -> Path:
    """根据 config 中的 task_id 找到正确的进度文件"""
    task_id = ""
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            task_id = cfg.get("batch_crawl", {}).get("task_id", "")
    except Exception:
        pass
    if not task_id:
        task_id = datetime.now().strftime("task_%Y%m%d")
    return PROJECT_ROOT / "data" / f"batch_progress_{task_id}.json"


def _get_task_summary() -> dict:
    """读取当前任务摘要"""
    summary = {"task_id": "", "excel_path": "", "total_creators": 0, "completed": 0, "failed": 0, "partial": 0, "remaining": 0}
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            batch_cfg = cfg.get("batch_crawl", {})
            summary["task_id"] = batch_cfg.get("task_id", "")
            summary["excel_path"] = batch_cfg.get("excel_path", "redbookaccontidandresult.xlsx")
    except Exception:
        pass
    if not summary["task_id"]:
        summary["task_id"] = datetime.now().strftime("task_%Y%m%d")

    try:
        excel_path = summary["excel_path"]
        if not os.path.isabs(excel_path):
            excel_path = str(PROJECT_ROOT / excel_path)
        if os.path.exists(excel_path):
            from tools.excel_reader import ExcelCreatorReader
            with ExcelCreatorReader(excel_path) as reader:
                summary["total_creators"] = len(reader.get_creators())
    except Exception:
        pass

    try:
        prog_file = PROJECT_ROOT / "data" / f"batch_progress_{summary['task_id']}.json"
        if prog_file.exists():
            with open(prog_file, "r", encoding="utf-8") as f:
                prog = json.load(f)
            summary["completed"] = len(prog.get("completed", []))
            summary["failed"] = len(prog.get("failed", {}))
            summary["partial"] = len(prog.get("partial", {}))
    except Exception:
        pass
    summary["remaining"] = max(0, summary["total_creators"] - summary["completed"])
    # partial 作者虽然有部分数据，但仍需重新爬取，也算在 remaining 里
    return summary


@router.get("/task-summary")
async def get_task_summary():
    """获取当前任务摘要"""
    return {"success": True, "data": _get_task_summary()}


@router.get("/batch/progress")
async def get_batch_progress():
    """获取批量爬取进度"""
    progress_file = _resolve_progress_path()
    task_id = progress_file.stem.replace("batch_progress_", "")
    if not progress_file.exists():
        return {"success": True, "data": {"completed": [], "failed": {}, "task_id": task_id}}
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["task_id"] = task_id
        return {"success": True, "data": data}
    except Exception:
        return {"success": True, "data": {"completed": [], "failed": {}, "task_id": task_id}}


@router.post("/batch/progress/clear-failed")
async def clear_failed_records():
    """清除失败记录"""
    progress_file = _resolve_progress_path()
    if not progress_file.exists():
        return {"success": True, "message": "无进度文件", "cleared": 0}
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        cleared = len(data.get("failed", {}))
        data["failed"] = {}
        data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"success": True, "message": f"已清除 {cleared} 条失败记录", "cleared": cleared}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.delete("/batch/progress")
async def reset_batch_progress():
    """重置全部进度"""
    progress_file = _resolve_progress_path()
    if progress_file.exists():
        progress_file.unlink()
    return {"success": True, "message": "进度已重置，下次将从头爬取全部作者"}


# ==================== 导出 ====================

@router.post("/export")
async def trigger_export(min_interaction: int = 0, export_format: str = "excel"):
    """手动触发导出"""
    global _batch_process, _batch_status

    if _batch_process and _batch_process.poll() is None:
        raise HTTPException(status_code=400, detail="爬取进行中，请等待完成")

    cmd = [
        "conda", "run", "--no-capture-output", "-n", "uvenv",
        "python", "-u", "-m", "tools.batch_crawler",
        "--export-only",
        "--min-interaction", str(min_interaction),
        "--export-format", export_format,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(PROJECT_ROOT),
            timeout=120,
        )
        return {
            "success": result.returncode == 0,
            "message": "导出完成" if result.returncode == 0 else "导出失败",
            "output": result.stdout[-1000:] if result.stdout else "",
            "error": result.stderr[-500:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="导出超时")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {e}")


# ==================== 数据文件 ====================

def _detect_file_status(filepath: Path, task_id: str = "") -> str:
    """判断文件状态: writing(正在写入) / completed(已完成) / unknown"""
    import time
    try:
        mtime = filepath.stat().st_mtime
        age = time.time() - mtime
        if age < 120:
            return "writing"
    except Exception:
        pass
    return "completed"


def _extract_nickname_from_json(filepath: Path) -> str:
    """从 JSON 文件中提取作者昵称（读取第一条记录的 nickname）"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data[0].get("nickname", "")
    except Exception:
        pass
    return ""


@router.get("/files")
async def list_export_files():
    """列出导出文件（含 task 目录下 per-creator 文件）"""
    files = []
    summary = _get_task_summary()
    task_id = summary.get("task_id", "")

    # 当前任务 task 目录下的 per-creator 文件
    if task_id:
        task_dir = PROJECT_ROOT / "data" / "xhs" / "json" / task_id
        if task_dir.exists() and task_dir.is_dir():
            for f in sorted(task_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.suffix == ".json":
                    stat = f.stat()
                    nickname = _extract_nickname_from_json(f)
                    note_count = 0
                    try:
                        with open(f, "r", encoding="utf-8") as fp:
                            arr = json.load(fp)
                        if isinstance(arr, list):
                            note_count = len(arr)
                    except Exception:
                        pass
                    status = _detect_file_status(f, task_id)
                    files.append({
                        "name": f.name,
                        "path": str(f.relative_to(PROJECT_ROOT)),
                        "abs_path": str(f),
                        "type": "creator_json",
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "status": status,
                        "nickname": nickname,
                        "note_count": note_count,
                        "task_id": task_id,
                        "viewable": True,
                    })

    # 汇总 JSON 数据文件（根目录）
    json_dir = PROJECT_ROOT / "data" / "xhs" / "json"
    if json_dir.exists():
        for f in sorted(json_dir.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
            if f.is_file() and f.suffix == ".json" and f.name.startswith("creator_contents"):
                stat = f.stat()
                status = _detect_file_status(f)
                files.append({
                    "name": f.name,
                    "path": str(f.relative_to(PROJECT_ROOT)),
                    "abs_path": str(f),
                    "type": "json",
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "status": status,
                    "nickname": "",
                    "note_count": 0,
                    "task_id": "",
                    "viewable": True,
                })

    # Excel 导出文件
    excel_dir = PROJECT_ROOT / "data" / "xiaohongshu" / "excel"
    if excel_dir.exists():
        for f in sorted(excel_dir.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
            if f.is_file() and f.suffix == ".xlsx":
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "path": str(f.relative_to(PROJECT_ROOT)),
                    "abs_path": str(f),
                    "type": "excel",
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "completed",
                    "nickname": "",
                    "note_count": 0,
                    "task_id": "",
                    "viewable": False,
                })

    return {"success": True, "data": files}


@router.get("/files/view")
async def view_file_content(path: str, offset: int = 0, limit: int = 50):
    """只读预览 JSON 文件内容，支持分页"""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / path

    # 安全检查：只允许读取 data 目录下的 JSON 文件
    try:
        resolved = resolved.resolve()
        data_root = (PROJECT_ROOT / "data").resolve()
        if not str(resolved).startswith(str(data_root)):
            raise HTTPException(status_code=403, detail="只允许查看 data 目录下的文件")
    except Exception as e:
        raise HTTPException(status_code=403, detail=f"路径不合法: {e}")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if resolved.suffix != ".json":
        raise HTTPException(status_code=400, detail="仅支持查看 JSON 文件")

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        # 正在写入的文件可能不是完整 JSON，尝试读取原始文本
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                raw = f.read()
            return {
                "success": True,
                "data": {
                    "type": "raw",
                    "content": raw[:10000],
                    "total": len(raw),
                    "truncated": len(raw) > 10000,
                    "parse_error": str(e),
                },
            }
        except Exception:
            raise HTTPException(status_code=500, detail=f"文件读取失败: {e}")

    if isinstance(data, list):
        total = len(data)
        page = data[offset : offset + limit]
        return {
            "success": True,
            "data": {
                "type": "list",
                "items": page,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < total,
                "fields": list(page[0].keys()) if page else [],
            },
        }
    elif isinstance(data, dict):
        return {
            "success": True,
            "data": {
                "type": "dict",
                "content": data,
                "total": len(data),
            },
        }
    else:
        return {"success": True, "data": {"type": "other", "content": str(data)[:5000]}}


# ==================== Session 管理 ====================

@router.get("/session/status")
async def get_session_status():
    """获取当前登录 session 状态（轻量级，不启动浏览器）"""
    import config as cfg

    result = {
        "browser_data_exists": False,
        "browser_data_size_mb": 0,
        "config_cookie": "",
        "config_cookie_raw": "",
        "config_login_type": getattr(cfg, "LOGIN_TYPE", "cookie"),
        "save_login_state": getattr(cfg, "SAVE_LOGIN_STATE", False),
        "crawler_running": False,
        "login_flow_status": "idle",
    }

    # 检查浏览器持久化目录
    try:
        user_data_dir_name = cfg.USER_DATA_DIR % "xhs"
        browser_data_dir = PROJECT_ROOT / "browser_data" / user_data_dir_name
        if browser_data_dir.exists():
            result["browser_data_exists"] = True
            total_size = sum(
                f.stat().st_size for f in browser_data_dir.rglob("*") if f.is_file()
            )
            result["browser_data_size_mb"] = round(total_size / 1024 / 1024, 1)
    except Exception:
        pass

    # 读取 config cookie（脱敏显示）
    cookie = getattr(cfg, "COOKIES", "") or ""
    # 提取 web_session 的值
    ws_val = ""
    if "web_session=" in cookie:
        ws_val = cookie.split("web_session=")[-1].split(";")[0].strip()
    elif cookie and "=" not in cookie:
        ws_val = cookie.strip()

    if len(ws_val) > 12:
        result["config_cookie"] = ws_val[:8] + "..." + ws_val[-4:]
    else:
        result["config_cookie"] = ws_val
    result["config_cookie_raw"] = ws_val

    # 爬虫进程状态：Dashboard 启动 / 控制面板启动 / 外部 CLI 启动
    if _batch_process and _batch_process.poll() is None:
        result["crawler_running"] = True
    else:
        try:
            from .control import _crawler_process, _detect_external_crawler
            if _crawler_process and _crawler_process.returncode is None:
                result["crawler_running"] = True
            elif _detect_external_crawler():
                result["crawler_running"] = True
        except Exception:
            pass

    # Login 路由的登录流程状态
    try:
        from .login import _login_state
        result["login_flow_status"] = _login_state.get("status", "idle")
    except Exception:
        pass

    return {"success": True, "data": result}


class CookieUpdateRequest(BaseModel):
    cookie: str


@router.post("/session/cookie")
async def update_session_cookie(request: CookieUpdateRequest):
    """更新 base_config.py 中的 COOKIES 值"""
    new_cookie = request.cookie.strip()
    if not new_cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")

    # 标准化格式：确保是 web_session=xxx 格式
    if "web_session=" not in new_cookie:
        new_cookie = f"web_session={new_cookie}"

    config_path = PROJECT_ROOT / "config" / "base_config.py"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="配置文件不存在")

    content = config_path.read_text(encoding="utf-8")

    # 兼容双引号和单引号两种格式
    pattern = re.compile(r"""COOKIES\s*=\s*(['"])(.*?)\1""")
    match = pattern.search(content)
    if not match:
        raise HTTPException(
            status_code=500,
            detail="未找到 COOKIES 配置项，请检查 base_config.py 格式",
        )

    old_value = match.group(2)
    if old_value != new_cookie:
        new_content = pattern.sub(f'COOKIES = "{new_cookie}"', content, count=1)
        config_path.write_text(new_content, encoding="utf-8")

    # 同时更新运行时 config
    import config as cfg
    cfg.COOKIES = new_cookie

    # 同步写入共享 cookie 文件，让 batch_crawler 子进程也能读到
    web_session_val = new_cookie.replace("web_session=", "").strip()
    cookie_dir = PROJECT_ROOT / "data" / "cookies"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    import time as _time
    shared_data = {
        "web_session": web_session_val,
        "cookie_str": new_cookie,
        "updated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "dashboard_ui",
    }
    try:
        shared_file = cookie_dir / "xhs_cookies.json"
        with open(shared_file, "w", encoding="utf-8") as f:
            json.dump(shared_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {"success": True, "message": f"Cookie 已更新 ({web_session_val[:8]}...)"}


@router.post("/session/verify")
async def verify_session():
    """
    完整验证 session 是否有效（启动浏览器检查，耗时约 5-10 秒）
    注意：爬虫运行时无法验证（浏览器数据目录被占用）
    """
    # 如果爬虫正在运行，无法启动第二个浏览器
    if _batch_process and _batch_process.poll() is None:
        return {
            "success": True,
            "data": {
                "status": "valid",
                "message": "爬虫正在运行，session 必定有效",
            },
        }

    import config as cfg

    try:
        user_data_dir_name = cfg.USER_DATA_DIR % "xhs"
        user_data_dir = str(PROJECT_ROOT / "browser_data" / user_data_dir_name)

        if not os.path.exists(user_data_dir):
            return {
                "success": True,
                "data": {
                    "status": "none",
                    "message": "未找到浏览器数据，需要首次登录",
                },
            }

        # 检查锁文件（Chromium 正在使用中）
        lock_file = os.path.join(user_data_dir, "SingletonLock")
        if os.path.exists(lock_file):
            return {
                "success": True,
                "data": {
                    "status": "locked",
                    "message": "浏览器数据目录被占用（可能有其他进程在使用）",
                },
            }

        from playwright.async_api import async_playwright
        from tools import utils

        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=True,
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )

            # 读取 cookie
            cookies = await ctx.cookies()
            _, cookie_dict = utils.convert_cookies(cookies)
            web_session = cookie_dict.get("web_session", "")

            if not web_session:
                await ctx.close()
                return {
                    "success": True,
                    "data": {
                        "status": "none",
                        "message": "浏览器中无 web_session，需要登录",
                    },
                }

            # 导航到小红书验证
            page = await ctx.new_page()
            stealth_path = str(PROJECT_ROOT / "libs" / "stealth.min.js")
            if os.path.exists(stealth_path):
                await page.add_init_script(path=stealth_path)

            await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
            await asyncio.sleep(3)

            is_valid = False
            try:
                me_selector = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"
                if await page.is_visible(me_selector, timeout=3000):
                    is_valid = True
                else:
                    qr = await page.query_selector("xpath=//img[@class='qrcode-img']")
                    if not qr:
                        is_valid = True
            except Exception:
                pass

            await ctx.close()

            masked = web_session[:8] + "..." + web_session[-4:] if len(web_session) > 12 else web_session
            return {
                "success": True,
                "data": {
                    "status": "valid" if is_valid else "expired",
                    "message": "Session 有效，可正常爬取" if is_valid else "Session 已过期，请重新登录",
                    "web_session": masked,
                },
            }
        finally:
            await pw.stop()

    except Exception as e:
        return {
            "success": True,
            "data": {
                "status": "error",
                "message": f"验证失败: {str(e)}",
            },
        }


# ==================== 飞书视频链接刷新 ====================

class RefreshVideoLinksRequest(BaseModel):
    app_token: str
    table_id: str = ""


@router.post("/feishu/refresh-video-links")
async def refresh_video_links(request: RefreshVideoLinksRequest):
    """刷新飞书视频汇总表中的临时公网下载链接"""
    import time as _time

    if not request.app_token.strip():
        raise HTTPException(status_code=400, detail="app_token 不能为空")

    # 读取飞书凭证
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        feishu_cfg = cfg.get("feishu", {})
        app_id = feishu_cfg.get("app_id", "")
        app_secret = feishu_cfg.get("app_secret", "")
        if not app_id or not app_secret:
            raise HTTPException(
                status_code=400,
                detail="飞书 App ID / App Secret 未配置，请先在飞书配置中填写",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")

    # 在线程中执行同步的飞书 API 调用（避免阻塞事件循环）
    def _do_refresh():
        from tools.feishu_bitable import FeishuBitableClient

        client = FeishuBitableClient(app_id, app_secret)
        try:
            app_token = request.app_token.strip()
            table_id = request.table_id.strip()

            # 自动查找"视频汇总"表
            if not table_id:
                tables = client.list_tables(app_token)
                for t in tables:
                    if "视频汇总" in t.get("name", ""):
                        table_id = t["table_id"]
                        break
                if not table_id:
                    return {
                        "success": False,
                        "message": "未找到「视频汇总」数据表，请手动指定 table_id",
                        "tables": tables,
                    }

            # 读取所有记录
            records = client.list_all_records(app_token, table_id)

            # 提取视频附件 file_token
            record_file_map = {}
            all_file_tokens = []
            for rec in records:
                record_id = rec.get("record_id", "")
                fields = rec.get("fields", {})
                video_attach = fields.get("视频附件")
                if video_attach and isinstance(video_attach, list):
                    for att in video_attach:
                        ft = att.get("file_token", "")
                        if ft:
                            record_file_map[record_id] = ft
                            all_file_tokens.append(ft)
                            break

            if not all_file_tokens:
                return {
                    "success": True,
                    "message": "汇总表中没有视频附件",
                    "total": len(records),
                    "videos": 0,
                    "updated": 0,
                }

            # 批量获取临时链接
            token_url_map = client.batch_get_tmp_download_url(all_file_tokens)

            # 确保"视频公网链接"字段存在
            try:
                client.add_field(app_token, table_id, "视频公网链接", 15)
            except Exception:
                pass  # 字段已存在

            # 批量更新
            update_records = []
            for record_id, file_token in record_file_map.items():
                tmp_url = token_url_map.get(file_token, "")
                if tmp_url:
                    update_records.append({
                        "record_id": record_id,
                        "fields": {
                            "视频公网链接": {"link": tmp_url, "text": tmp_url}
                        },
                    })

            updated = 0
            if update_records:
                updated = client.batch_update_records(
                    app_token, table_id, update_records
                )

            return {
                "success": True,
                "message": f"已刷新 {updated}/{len(all_file_tokens)} 条视频链接",
                "total": len(records),
                "videos": len(all_file_tokens),
                "updated": updated,
                "table_id": table_id,
            }
        finally:
            client.close()

    start = _time.time()
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _do_refresh)
        elapsed = round(_time.time() - start, 1)
        result["elapsed_sec"] = elapsed
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新失败: {e}")


# ==================== 视频脚本提取 ====================

# 后台任务状态
_script_task = {"status": "idle", "progress": "", "result": None}


class ExtractScriptsRequest(BaseModel):
    app_token: str
    table_id: str = ""
    skip_existing: bool = True
    gemini_base_url: str = "https://ops-ai-gateway.yc345.tv/v1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-pro-preview"
    prompt: str = ""


@router.post("/feishu/extract-scripts")
async def extract_video_scripts(request: ExtractScriptsRequest):
    """启动视频脚本提取任务（后台运行）"""
    global _script_task

    if _script_task["status"] == "running":
        return {
            "success": False,
            "message": "脚本提取任务正在运行中，请等待完成",
            "progress": _script_task["progress"],
        }

    if not request.app_token.strip():
        raise HTTPException(status_code=400, detail="app_token 不能为空")
    if not request.gemini_api_key.strip():
        raise HTTPException(status_code=400, detail="Gemini API Key 不能为空")

    # 读取飞书凭证
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        feishu_cfg = cfg.get("feishu", {})
        app_id = feishu_cfg.get("app_id", "")
        app_secret = feishu_cfg.get("app_secret", "")
        if not app_id or not app_secret:
            raise HTTPException(
                status_code=400,
                detail="飞书 App ID / App Secret 未配置",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")

    # 在后台线程执行
    def _do_extract():
        global _script_task

        def on_progress(current, total, title):
            _script_task["progress"] = f"[{current}/{total}] {title}"

        try:
            from tools.video_script_extractor import VideoScriptExtractor

            kwargs = dict(
                feishu_app_id=app_id,
                feishu_app_secret=app_secret,
                gemini_base_url=request.gemini_base_url.strip(),
                gemini_api_key=request.gemini_api_key.strip(),
                gemini_model=request.gemini_model.strip(),
            )
            if request.prompt.strip():
                kwargs["prompt"] = request.prompt.strip()

            with VideoScriptExtractor(**kwargs) as extractor:
                result = extractor.extract_and_write(
                    app_token=request.app_token.strip(),
                    table_id=request.table_id.strip(),
                    skip_existing=request.skip_existing,
                    on_progress=on_progress,
                )
            _script_task["status"] = "completed"
            _script_task["result"] = result
        except Exception as e:
            _script_task["status"] = "error"
            _script_task["result"] = {"success": False, "message": f"任务异常: {e}"}

    _script_task = {"status": "running", "progress": "初始化中...", "result": None}

    import threading
    thread = threading.Thread(target=_do_extract, daemon=True)
    thread.start()

    return {"success": True, "message": "脚本提取任务已启动"}


@router.get("/feishu/extract-scripts/status")
async def get_script_extract_status():
    """获取脚本提取任务状态"""
    return {"success": True, "data": _script_task}
