# -*- coding: utf-8 -*-
"""
任务管理 API —— 提供任务的 CRUD、启动、停止、状态查询
每个任务对应 config/tasks/{task_id}.json，运行时 spawn 子进程执行 main.py

数据隔离说明：
- 本接口启动的任务仅通过命令行参数驱动 main.py，不会修改以下数据：
  - data/batch_progress_*.json（18 位作者等批量任务的进度）
  - data/xhs/progress/creator_*_progress.json（各作者增量进度）
  - config/anti_crawl_config.json（不会回写）
- 新任务与「18 位作者」增量爬取互不影响；作者进度与数据请勿删除，单独保留。
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/dashboard/tasks", tags=["tasks"])

_TASKS_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "config" / "tasks"
_TASKS_DIR.mkdir(parents=True, exist_ok=True)
_TASK_LOGS_DIR = _TASKS_DIR.parent / "task_logs"
_TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 运行时状态 ====================

class _RunningTask:
    __slots__ = ("process", "started_at", "logs", "_log_id", "_read_task", "_log_queue")

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs: List[Dict] = []
        self._log_id = 0
        self._read_task: Optional[asyncio.Task] = None
        self._log_queue: Optional[asyncio.Queue] = None


_running: Dict[str, _RunningTask] = {}
_lock = asyncio.Lock()

# ==================== Pydantic 模型 ====================

class TaskConfig(BaseModel):
    task_id: str = Field("", description="自动生成，创建时可不填")
    name: str = ""
    platform: str = "xhs"
    crawler_type: str = "search"
    keywords: str = ""
    keywords_combine_mode: bool = False
    creator_urls: List[str] = []
    max_notes_per_keyword: int = 80
    sort_type: str = "general"
    date_start: str = ""
    date_end: str = ""
    enable_comments: bool = False
    max_comments: int = 50
    enable_sub_comments: bool = False
    max_sub_comments: int = 30
    max_sub_comments_per_comment: int = 30
    save_data_option: str = "json"
    cookies: str = ""
    headless: bool = True
    notes: str = ""
    top_notes_count: int = 100
    top_comment_notes_count: int = 20
    comment_page_count: int = 2
    feishu_folder_token: str = ""
    filter_keywords: str = ""
    filter_scope: str = "title,desc,tags"


class TaskConfigUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    crawler_type: Optional[str] = None
    keywords: Optional[str] = None
    keywords_combine_mode: Optional[bool] = None
    creator_urls: Optional[List[str]] = None
    max_notes_per_keyword: Optional[int] = None
    sort_type: Optional[str] = None
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    enable_comments: Optional[bool] = None
    max_comments: Optional[int] = None
    enable_sub_comments: Optional[bool] = None
    max_sub_comments: Optional[int] = None
    max_sub_comments_per_comment: Optional[int] = None
    save_data_option: Optional[str] = None
    cookies: Optional[str] = None
    headless: Optional[bool] = None
    notes: Optional[str] = None
    top_notes_count: Optional[int] = None
    top_comment_notes_count: Optional[int] = None
    comment_page_count: Optional[int] = None
    feishu_folder_token: Optional[str] = None
    filter_keywords: Optional[str] = None
    filter_scope: Optional[str] = None

# ==================== 辅助函数 ====================

def _task_path(task_id: str) -> Path:
    return _TASKS_DIR / f"{task_id}.json"


def _sanitize_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]", "_", name.strip())[:40]
    return slug or "task"


def _generate_task_id(name: str) -> str:
    base = _sanitize_id(name) if name else "task"
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = f"{base}_{ts}"
    if not _task_path(candidate).exists():
        return candidate
    for i in range(1, 100):
        c = f"{candidate}_{i}"
        if not _task_path(c).exists():
            return c
    return candidate


def _read_task_json(task_id: str) -> dict:
    p = _task_path(task_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def _write_task_json(task_id: str, data: dict):
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _task_path(task_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _compute_status(task_id: str, stored_status: str) -> str:
    rt = _running.get(task_id)
    if rt and rt.process.poll() is None:
        return "running"
    if rt and rt.process.poll() is not None:
        return "completed" if rt.process.returncode == 0 else "error"
    return stored_status if stored_status in ("idle", "completed", "error") else "idle"


_PROJECT_ROOT = str(Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))


def _build_cmd(data: dict) -> List[str]:
    cmd = ["conda", "run", "--no-capture-output", "-n", "uvenv", "--cwd", _PROJECT_ROOT, "python", "-u", "main.py"]
    cmd.extend(["--platform", data.get("platform", "xhs")])
    cmd.extend(["--lt", "cookie"])
    cmd.extend(["--type", data.get("crawler_type", "search")])
    cmd.extend(["--save_data_option", data.get("save_data_option", "json")])

    if data.get("sort_type"):
        cmd.extend(["--sort_type", data["sort_type"]])
    if data.get("max_notes_per_keyword"):
        cmd.extend(["--max_notes", str(data["max_notes_per_keyword"])])
    if data.get("date_start"):
        cmd.extend(["--date_start", data["date_start"]])
    if data.get("date_end"):
        cmd.extend(["--date_end", data["date_end"]])

    ct = data.get("crawler_type", "search")
    # 搜索模式：有关键词则传入，否则不传，由 main 从 config/search_keyword_pool.json 加载
    if ct in ("search", "search_top") and data.get("keywords"):
        cmd.extend(["--keywords", data["keywords"]])
        if data.get("keywords_combine_mode"):
            cmd.append("--keywords-combine")
    elif ct == "creator" and data.get("creator_urls"):
        cmd.extend(["--creator_id", ",".join(data["creator_urls"])])
    elif ct == "creator_keyword":
        if data.get("creator_urls"):
            cmd.extend(["--creator_id", ",".join(data["creator_urls"])])
        if data.get("filter_keywords"):
            cmd.extend(["--filter_keywords", data["filter_keywords"]])
        if data.get("filter_scope"):
            cmd.extend(["--filter_scope", data["filter_scope"]])

    if ct == "search_top":
        if data.get("top_notes_count"):
            cmd.extend(["--top_notes_count", str(data["top_notes_count"])])
        if data.get("top_comment_notes_count"):
            cmd.extend(["--top_comment_notes_count", str(data["top_comment_notes_count"])])
        if data.get("comment_page_count"):
            cmd.extend(["--comment_page_count", str(data["comment_page_count"])])

    cmd.extend(["--get_comment", "true" if data.get("enable_comments") else "false"])
    cmd.extend(["--get_sub_comment", "true" if data.get("enable_sub_comments") else "false"])

    if data.get("max_comments"):
        cmd.extend(["--max_comments_count_singlenotes", str(data["max_comments"])])
    if data.get("max_sub_comments_per_comment") is not None:
        cmd.extend(["--max_sub_comments_per_comment", str(data["max_sub_comments_per_comment"])])

    # 不传任务里存的 cookies，统一用「当前最新」登录态（共享 cookie 文件 / 浏览器 session），
    # 避免任务里存的旧 cookie 覆盖刚在模拟界面登录的新 session，导致“刚登录却报过期”
    # if data.get("cookies"):
    #     cmd.extend(["--cookies", data["cookies"]])

    cmd.extend(["--headless", "true" if data.get("headless", True) else "false"])

    if data.get("feishu_folder_token"):
        cmd.extend(["--feishu_folder", data["feishu_folder_token"]])

    return cmd


async def _stream_output(task_id: str, rt: _RunningTask):
    loop = asyncio.get_event_loop()
    log_file_path = _TASK_LOGS_DIR / f"{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_fh = None
    feishu_status = ""  # "", "writing", "success", "failed"
    feishu_url = ""
    feishu_error = ""
    try:
        log_fh = open(log_file_path, "a", encoding="utf-8")

        def _append_line(line_text: str):
            nonlocal feishu_status, feishu_url, feishu_error
            ts = datetime.now().strftime("%H:%M:%S")
            rt._log_id += 1
            rt.logs.append({"id": rt._log_id, "timestamp": ts, "message": line_text})
            if len(rt.logs) > 2000:
                rt.logs = rt.logs[-1500:]
            if log_fh:
                log_fh.write(f"[{ts}] {line_text}\n")
                log_fh.flush()
            if "[Main] 开始写入飞书" in line_text:
                feishu_status = "writing"
            elif "[Main] 飞书写入完成" in line_text:
                feishu_status = "success"
                idx = line_text.find("http")
                if idx >= 0:
                    feishu_url = line_text[idx:].strip()
            elif "[Main] 飞书写入失败" in line_text:
                feishu_status = "failed"
                feishu_error = line_text.split("飞书写入失败:")[-1].strip() if "飞书写入失败:" in line_text else line_text

        while rt.process and rt.process.poll() is None:
            line = await loop.run_in_executor(None, rt.process.stdout.readline)
            if line:
                line = line.strip()
                if line:
                    _append_line(line)

        if rt.process and rt.process.stdout:
            remaining = await loop.run_in_executor(None, rt.process.stdout.read)
            if remaining:
                for line in remaining.strip().split("\n"):
                    if line.strip():
                        _append_line(line.strip())

        exit_code = rt.process.returncode if rt.process else -1
        new_status = "completed" if exit_code == 0 else "error"
        _append_line(f"=== 任务结束: exit_code={exit_code}, status={new_status} ===")
        try:
            data = _read_task_json(task_id)
            data["status"] = new_status
            data["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if feishu_status:
                data["feishu_status"] = feishu_status
            if feishu_url:
                data["feishu_url"] = feishu_url
            if feishu_error:
                data["feishu_error"] = feishu_error
            _write_task_json(task_id, data)
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass

# ==================== CRUD 接口 ====================

@router.get("")
async def list_tasks():
    tasks = []
    for f in sorted(_TASKS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["status"] = _compute_status(data.get("task_id", f.stem), data.get("status", "idle"))
            tasks.append(data)
        except Exception:
            continue
    return {"success": True, "tasks": tasks}


@router.post("")
async def create_task(body: TaskConfig):
    task_id = _generate_task_id(body.name)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = body.model_dump()
    data["task_id"] = task_id
    data["created_at"] = now
    data["updated_at"] = now
    data["status"] = "idle"
    data["last_run_at"] = ""
    _write_task_json(task_id, data)
    return {"success": True, "task_id": task_id, "message": f"任务 {body.name or task_id} 已创建"}


@router.get("/{task_id}")
async def get_task(task_id: str):
    data = _read_task_json(task_id)
    data["status"] = _compute_status(task_id, data.get("status", "idle"))
    return {"success": True, "task": data}


@router.put("/{task_id}")
async def update_task(task_id: str, body: TaskConfigUpdate):
    data = _read_task_json(task_id)
    if _compute_status(task_id, data.get("status", "idle")) == "running":
        raise HTTPException(status_code=400, detail="任务正在运行，无法修改")
    updates = body.model_dump(exclude_none=True)
    data.update(updates)
    _write_task_json(task_id, data)
    return {"success": True, "message": "任务已更新"}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    data = _read_task_json(task_id)
    if _compute_status(task_id, data.get("status", "idle")) == "running":
        raise HTTPException(status_code=400, detail="任务正在运行，请先停止")
    _task_path(task_id).unlink(missing_ok=True)
    _running.pop(task_id, None)
    return {"success": True, "message": f"任务 {task_id} 已删除"}


# ==================== 导出 / 导入 ====================

@router.get("/{task_id}/export")
async def export_task(task_id: str):
    """导出任务配置为 JSON 文件，便于备份或迁移后导入使用。"""
    data = _read_task_json(task_id)
    # 导出时去掉运行时状态，便于跨环境导入
    export_data = {k: v for k, v in data.items() if k not in ("status", "last_run_at")}
    body = json.dumps(export_data, ensure_ascii=False, indent=2)
    filename = f"{task_id}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _import_task_from_data(data: dict) -> dict:
    """共用逻辑：根据导入的 data 写入新任务并返回 task_id."""
    data = {k: v for k, v in data.items() if k not in ("task_id", "status", "created_at", "updated_at", "last_run_at")}
    name = data.get("name", "") or "导入任务"
    task_id = _generate_task_id(name)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["task_id"] = task_id
    data["created_at"] = now
    data["updated_at"] = now
    data["status"] = "idle"
    data["last_run_at"] = ""
    _write_task_json(task_id, data)
    return {"success": True, "task_id": task_id, "message": f"已导入为任务 {task_id}，可在此任务上继续使用"}


@router.post("/import")
async def import_task(body: dict):
    """
    从 JSON 请求体导入任务（Content-Type: application/json）。
    导入后会生成新 task_id，不会覆盖已有任务。
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体须为 JSON 对象")
    return _import_task_from_data(body)


@router.post("/import/file")
async def import_task_file(file: UploadFile = File(..., description="任务 JSON 文件")):
    """
    上传任务 JSON 文件并导入。导入后会生成新 task_id，不会覆盖已有任务。
    """
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"上传文件不是合法 JSON: {e}")
    return _import_task_from_data(data)


# ==================== 启停接口 ====================

@router.post("/{task_id}/start")
async def start_task(task_id: str):
    async with _lock:
        data = _read_task_json(task_id)
        if _compute_status(task_id, data.get("status", "idle")) == "running":
            raise HTTPException(status_code=400, detail="任务已在运行中")

        cmd = _build_cmd(data)
        # 每个任务使用独立浏览器数据目录，避免多任务并行时 Playwright 锁冲突
        task_env = os.environ.copy()
        task_env["MC_INSTANCE_ID"] = task_id
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=task_env,
                cwd=_PROJECT_ROOT
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"启动失败: {e}")

        rt = _RunningTask(proc)
        _running[task_id] = rt

        data["status"] = "running"
        data["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_task_json(task_id, data)

        rt._read_task = asyncio.create_task(_stream_output(task_id, rt))

    return {"success": True, "message": f"任务 {task_id} 已启动", "pid": proc.pid}


def _log_stop_audit(request: Request, task_id: str):
    client = getattr(request, "client", None)
    ip = client.host if client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or ip
    ua = request.headers.get("user-agent", "")[:80]
    logging.getLogger("MediaCrawler").info(
        f"[StopAudit] 任务停止请求 task_id={task_id} | 来源={forwarded} | UA={ua}"
    )


@router.post("/{task_id}/stop")
async def stop_task(task_id: str, request: Request):
    _log_stop_audit(request, task_id)
    async with _lock:
        rt = _running.get(task_id)
        if not rt or rt.process.poll() is not None:
            raise HTTPException(status_code=400, detail="任务未在运行")

        try:
            rt.process.send_signal(signal.SIGTERM)
            for _ in range(30):
                if rt.process.poll() is not None:
                    break
                await asyncio.sleep(0.5)
            if rt.process.poll() is None:
                rt.process.kill()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"停止失败: {e}")

        if rt._read_task:
            rt._read_task.cancel()
            rt._read_task = None

        try:
            data = _read_task_json(task_id)
            data["status"] = "idle"
            _write_task_json(task_id, data)
        except Exception:
            pass

    return {"success": True, "message": f"任务 {task_id} 已停止"}


@router.get("/{task_id}/status")
async def task_status(task_id: str):
    data = _read_task_json(task_id)
    status = _compute_status(task_id, data.get("status", "idle"))
    rt = _running.get(task_id)
    logs = rt.logs[-200:] if rt else []
    pid = rt.process.pid if rt and rt.process else None
    return {
        "success": True,
        "task_id": task_id,
        "status": status,
        "last_run_at": data.get("last_run_at", ""),
        "pid": pid,
        "logs": logs,
        "feishu_status": data.get("feishu_status", ""),
        "feishu_url": data.get("feishu_url", ""),
        "feishu_error": data.get("feishu_error", ""),
    }


# ==================== 飞书重新写入 ====================

_resync_running: Dict[str, subprocess.Popen] = {}


@router.post("/{task_id}/resync-feishu")
async def resync_feishu(task_id: str):
    """一键重新写入飞书：从本地已有 JSON 数据推送到飞书多维表格"""
    if task_id in _resync_running:
        p = _resync_running[task_id]
        if p.poll() is None:
            return {"success": False, "detail": "飞书写入正在进行中，请稍候"}

    data = _read_task_json(task_id)
    ct = data.get("crawler_type", "search")
    if ct not in ("search", "search_top"):
        raise HTTPException(status_code=400, detail="仅搜索类任务支持飞书写入")

    platform = data.get("platform", "xhs")
    json_dir = Path(_PROJECT_ROOT) / "data" / platform / "json"
    prefix = "search_top_contents_" if ct == "search_top" else "search_contents_"
    candidates = sorted(
        [f for f in json_dir.glob(f"{prefix}*.json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if json_dir.is_dir() else []

    if not candidates:
        raise HTTPException(status_code=404, detail=f"未找到 {prefix}*.json 数据文件")

    latest = candidates[0].name
    session_ts = latest[len(prefix):-5]

    folder_token = data.get("feishu_folder_token", "")
    resync_cmd = [
        "conda", "run", "--no-capture-output", "-n", "uvenv",
        "--cwd", _PROJECT_ROOT,
        "python", "-m", "tools.resync_feishu_search", session_ts,
    ]
    if folder_token:
        resync_cmd.extend(["--folder", folder_token])

    data["feishu_status"] = "writing"
    data["feishu_error"] = ""
    _write_task_json(task_id, data)

    loop = asyncio.get_event_loop()

    proc = subprocess.Popen(
        resync_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_PROJECT_ROOT,
    )
    _resync_running[task_id] = proc

    async def _wait_resync():
        output_lines = []
        try:
            while proc.poll() is None:
                line = await loop.run_in_executor(None, proc.stdout.readline)
                if line:
                    output_lines.append(line.strip())
            remaining = await loop.run_in_executor(None, proc.stdout.read)
            if remaining:
                output_lines.extend(remaining.strip().split("\n"))
        except Exception:
            pass

        full_output = "\n".join(output_lines)
        try:
            d = _read_task_json(task_id)
            if "飞书写入完成" in full_output:
                d["feishu_status"] = "success"
                for ln in output_lines:
                    if "http" in ln:
                        idx = ln.find("http")
                        if idx >= 0:
                            d["feishu_url"] = ln[idx:].strip()
                            break
            elif "飞书写入失败" in full_output:
                d["feishu_status"] = "failed"
                d["feishu_error"] = full_output[-500:] if len(full_output) > 500 else full_output
            elif proc.returncode != 0:
                d["feishu_status"] = "failed"
                d["feishu_error"] = full_output[-500:] if len(full_output) > 500 else full_output
            else:
                d["feishu_status"] = "success"
            _write_task_json(task_id, d)
        except Exception:
            pass
        _resync_running.pop(task_id, None)

    asyncio.create_task(_wait_resync())
    return {"success": True, "message": f"飞书写入已启动 (session: {session_ts})"}


# ==================== 进度快照（Checkpoint）接口 ====================


@router.get("/checkpoints/list")
async def list_all_checkpoints():
    """列出所有快照"""
    from tools.checkpoint_manager import list_checkpoints
    return list_checkpoints()


@router.get("/{task_id}/checkpoints")
async def list_task_checkpoints(task_id: str):
    """列出指定任务的快照"""
    from tools.checkpoint_manager import list_checkpoints
    return list_checkpoints(task_id)


@router.post("/{task_id}/checkpoint/export")
async def export_task_checkpoint(task_id: str, body: dict = None):
    """导出当前爬取进度为快照"""
    from tools.checkpoint_manager import export_checkpoint
    body = body or {}
    try:
        path = export_checkpoint(
            task_id=task_id,
            feishu_url=body.get("feishu_url", ""),
            note=body.get("note", "手动导出"),
        )
        filename = os.path.basename(path)
        from tools.checkpoint_manager import get_checkpoint_detail
        detail = get_checkpoint_detail(filename)
        return {
            "success": True,
            "filename": filename,
            "summary": detail.get("summary", {}),
            "message": f"快照已导出: {filename}",
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {e}")


@router.post("/{task_id}/checkpoint/import")
async def import_task_checkpoint(task_id: str, body: dict = None):
    """从快照恢复 batch_progress，作为增量爬取基线"""
    from tools.checkpoint_manager import import_checkpoint
    body = body or {}
    filename = body.get("filename", "")
    if not filename:
        raise HTTPException(status_code=400, detail="请指定 filename")
    from tools.checkpoint_manager import _CHECKPOINT_DIR
    filepath = os.path.join(_CHECKPOINT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"快照文件不存在: {filename}")
    try:
        result = import_checkpoint(filepath)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导入失败: {e}")


@router.get("/checkpoints/detail/{filename}")
async def get_checkpoint(filename: str):
    """获取快照详情"""
    from tools.checkpoint_manager import get_checkpoint_detail
    try:
        return get_checkpoint_detail(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"快照不存在: {filename}")


@router.delete("/checkpoints/{filename}")
async def delete_checkpoint(filename: str):
    """删除快照"""
    from tools.checkpoint_manager import _CHECKPOINT_DIR
    filepath = os.path.join(_CHECKPOINT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"快照不存在: {filename}")
    os.remove(filepath)
    return {"success": True, "message": f"已删除: {filename}"}
