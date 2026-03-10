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
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/dashboard/tasks", tags=["tasks"])

_TASKS_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "config" / "tasks"
_TASKS_DIR.mkdir(parents=True, exist_ok=True)

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
    creator_urls: List[str] = []
    max_notes_per_keyword: int = 80
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


class TaskConfigUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    crawler_type: Optional[str] = None
    keywords: Optional[str] = None
    creator_urls: Optional[List[str]] = None
    max_notes_per_keyword: Optional[int] = None
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


def _build_cmd(data: dict) -> List[str]:
    cmd = ["conda", "run", "--no-capture-output", "-n", "uvenv", "python", "-u", "main.py"]
    cmd.extend(["--platform", data.get("platform", "xhs")])
    cmd.extend(["--lt", "cookie"])
    cmd.extend(["--type", data.get("crawler_type", "search")])
    cmd.extend(["--save_data_option", data.get("save_data_option", "json")])

    ct = data.get("crawler_type", "search")
    # 搜索模式：有关键词则传入，否则不传，由 main 从 config/search_keyword_pool.json 加载
    if ct in ("search", "search_top") and data.get("keywords"):
        cmd.extend(["--keywords", data["keywords"]])
    elif ct == "creator" and data.get("creator_urls"):
        cmd.extend(["--creator_id", ",".join(data["creator_urls"])])

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
    try:
        while rt.process and rt.process.poll() is None:
            line = await loop.run_in_executor(None, rt.process.stdout.readline)
            if line:
                line = line.strip()
                if line:
                    rt._log_id += 1
                    entry = {
                        "id": rt._log_id,
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "message": line,
                    }
                    rt.logs.append(entry)
                    if len(rt.logs) > 2000:
                        rt.logs = rt.logs[-1500:]

        if rt.process and rt.process.stdout:
            remaining = await loop.run_in_executor(None, rt.process.stdout.read)
            if remaining:
                for line in remaining.strip().split("\n"):
                    if line.strip():
                        rt._log_id += 1
                        rt.logs.append({
                            "id": rt._log_id,
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                            "message": line.strip(),
                        })

        exit_code = rt.process.returncode if rt.process else -1
        new_status = "completed" if exit_code == 0 else "error"
        try:
            data = _read_task_json(task_id)
            data["status"] = new_status
            data["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_task_json(task_id, data)
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
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
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
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


@router.post("/{task_id}/stop")
async def stop_task(task_id: str):
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
    }
