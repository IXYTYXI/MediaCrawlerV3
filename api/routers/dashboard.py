# -*- coding: utf-8 -*-
"""
Dashboard API - 配置管理 + 批量爬取控制
提供前端 Dashboard 所需的所有接口
"""
import asyncio
import json
import os
import subprocess
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
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


@router.post("/batch/start")
async def start_batch_crawl(request: BatchStartRequest):
    """启动批量爬取"""
    global _batch_process, _batch_status, _batch_logs

    if _batch_process and _batch_process.poll() is None:
        raise HTTPException(status_code=400, detail="批量爬取正在运行中")

    _batch_logs = []

    # 构建命令
    cmd = ["uv", "run", "python", "-m", "tools.batch_crawler"]
    if request.skip_feishu:
        cmd.append("--skip-feishu")
    if request.limit > 0:
        cmd.extend(["--limit", str(request.limit)])
    cmd.extend(["--min-interaction", str(request.min_interaction)])
    cmd.extend(["--max-notes", str(request.max_notes)])
    if request.enable_comments:
        cmd.append("--enable-comments")

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
    """停止批量爬取"""
    global _batch_process, _batch_status

    if not _batch_process or _batch_process.poll() is not None:
        raise HTTPException(status_code=400, detail="没有正在运行的批量爬取")

    try:
        _batch_process.send_signal(signal.SIGTERM)
        for _ in range(30):
            if _batch_process.poll() is not None:
                break
            await asyncio.sleep(0.5)
        if _batch_process.poll() is None:
            _batch_process.kill()
        _batch_status = {"status": "idle", "started_at": None, "message": "已停止"}
        return {"success": True, "message": "批量爬取已停止"}
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


@router.get("/batch/logs")
async def get_batch_logs(limit: int = 100, offset: int = 0):
    """获取批量爬取日志"""
    logs = _batch_logs[offset:offset + limit] if limit > 0 else _batch_logs[offset:]
    return {"success": True, "data": logs, "total": len(_batch_logs)}


async def _read_batch_output():
    """后台读取批量爬取输出"""
    global _batch_process, _batch_status, _batch_logs
    loop = asyncio.get_event_loop()
    try:
        while _batch_process and _batch_process.poll() is None:
            line = await loop.run_in_executor(None, _batch_process.stdout.readline)
            if line:
                _batch_logs.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": line.strip(),
                })
        # 读取剩余
        if _batch_process and _batch_process.stdout:
            remaining = await loop.run_in_executor(None, _batch_process.stdout.read)
            if remaining:
                for line in remaining.strip().split("\n"):
                    if line.strip():
                        _batch_logs.append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "message": line.strip(),
                        })
    except Exception:
        pass


# ==================== 批量进度 ====================

@router.get("/batch/progress")
async def get_batch_progress():
    """获取批量爬取进度（已完成的作者）"""
    if not BATCH_PROGRESS_PATH.exists():
        return {"success": True, "data": {"completed": [], "failed": {}}}
    try:
        with open(BATCH_PROGRESS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": True, "data": {"completed": [], "failed": {}}}


@router.delete("/batch/progress")
async def reset_batch_progress():
    """重置批量爬取进度"""
    if BATCH_PROGRESS_PATH.exists():
        BATCH_PROGRESS_PATH.unlink()
    return {"success": True, "message": "进度已重置"}


# ==================== 导出 ====================

@router.post("/export")
async def trigger_export(min_interaction: int = 0, export_format: str = "excel"):
    """手动触发导出"""
    global _batch_process, _batch_status

    if _batch_process and _batch_process.poll() is None:
        raise HTTPException(status_code=400, detail="爬取进行中，请等待完成")

    cmd = [
        "uv", "run", "python", "-m", "tools.batch_crawler",
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

@router.get("/files")
async def list_export_files():
    """列出导出文件"""
    files = []

    # JSON 数据文件
    json_dir = PROJECT_ROOT / "data" / "xhs" / "json"
    if json_dir.exists():
        for f in sorted(json_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix == ".json" and f.name.startswith("creator_contents"):
                files.append({
                    "name": f.name,
                    "path": str(f),
                    "type": "json",
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                })

    # Excel 导出文件
    excel_dir = PROJECT_ROOT / "data" / "xiaohongshu" / "excel"
    if excel_dir.exists():
        for f in sorted(excel_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix == ".xlsx":
                files.append({
                    "name": f.name,
                    "path": str(f),
                    "type": "excel",
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                })

    return {"success": True, "data": files}
