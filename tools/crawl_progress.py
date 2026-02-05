# -*- coding: utf-8 -*-
"""
断点续爬进度管理模块

功能：
1. 保存已爬取的 note_id 到进度文件
2. 启动时加载进度，跳过已爬取的内容
3. 异常退出时自动保存进度

性能优化：
- 使用 Set（哈希集合）存储，查找复杂度 O(1)
- 支持 SQLite 存储模式，适合百万级数据
- 增量保存，减少 IO 开销
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Set, Optional
from pathlib import Path

from config import base_config as config
from tools import utils


class CrawlProgressManager:
    """
    爬取进度管理器
    
    性能说明：
    - 使用 Set（哈希集合）存储 note_id，查找复杂度 O(1)
    - 比二分法 O(log n) 更快
    - 10万条数据查找只需 1 次哈希计算
    
    存储模式：
    - json: 适合小数据量（<10万），简单易读
    - sqlite: 适合大数据量（>10万），更省内存
    """
    
    def __init__(self, platform: str = "xhs", crawler_type: str = "creator", use_sqlite: bool = False):
        """
        初始化进度管理器
        
        Args:
            platform: 平台名称
            crawler_type: 爬取类型 (creator/search/detail)
            use_sqlite: 是否使用 SQLite 存储（大数据量推荐）
        """
        self.platform = platform
        self.crawler_type = crawler_type
        self.use_sqlite = use_sqlite
        self.progress_dir = Path("data") / platform / "progress"
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        
        # 当前会话的进度 - 使用 Set，O(1) 查找
        self._crawled_ids: Set[str] = set()  # 作品详情已爬取
        self._failed_ids: Set[str] = set()   # 作品详情失败
        self._comment_crawled_ids: Set[str] = set()  # 评论已爬取
        self._current_task_id: Optional[str] = None
        self._start_time: Optional[str] = None
        self._db_conn: Optional[sqlite3.Connection] = None
        
        # 增量保存缓冲
        self._pending_crawled: Set[str] = set()
        self._pending_failed: Set[str] = set()
        self._pending_comment_crawled: Set[str] = set()
        
    def get_progress_file(self, task_id: str) -> Path:
        """获取进度文件路径（JSON 模式）"""
        # task_id 可以是 user_id（creator模式）或 keyword（search模式）
        safe_task_id = task_id.replace("/", "_").replace(":", "_").replace("?", "_")
        return self.progress_dir / f"{self.crawler_type}_{safe_task_id}_progress.json"
    
    def get_db_file(self, task_id: str) -> Path:
        """获取数据库文件路径（SQLite 模式）"""
        safe_task_id = task_id.replace("/", "_").replace(":", "_").replace("?", "_")
        return self.progress_dir / f"{self.crawler_type}_{safe_task_id}_progress.db"
    
    def _init_sqlite(self, task_id: str):
        """初始化 SQLite 数据库"""
        db_file = self.get_db_file(task_id)
        self._db_conn = sqlite3.connect(str(db_file))
        cursor = self._db_conn.cursor()
        
        # 创建表（如果不存在）- 使用索引加速查询
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS crawled_ids (
                note_id TEXT PRIMARY KEY
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS failed_ids (
                note_id TEXT PRIMARY KEY
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        self._db_conn.commit()
    
    def _load_from_sqlite(self) -> Set[str]:
        """从 SQLite 加载已爬取的 ID"""
        if not self._db_conn:
            return set()
        
        cursor = self._db_conn.cursor()
        cursor.execute("SELECT note_id FROM crawled_ids")
        self._crawled_ids = {row[0] for row in cursor.fetchall()}
        
        cursor.execute("SELECT note_id FROM failed_ids")
        self._failed_ids = {row[0] for row in cursor.fetchall()}
        
        return self._crawled_ids
    
    def _save_to_sqlite_incremental(self):
        """增量保存到 SQLite（只保存新增的）"""
        if not self._db_conn:
            return
        
        cursor = self._db_conn.cursor()
        
        # 批量插入新增的 crawled_ids
        if self._pending_crawled:
            cursor.executemany(
                "INSERT OR IGNORE INTO crawled_ids (note_id) VALUES (?)",
                [(id,) for id in self._pending_crawled]
            )
            self._pending_crawled.clear()
        
        # 批量插入新增的 failed_ids
        if self._pending_failed:
            cursor.executemany(
                "INSERT OR IGNORE INTO failed_ids (note_id) VALUES (?)",
                [(id,) for id in self._pending_failed]
            )
            self._pending_failed.clear()
        
        # 更新元数据
        cursor.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("last_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        cursor.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("crawled_count", str(len(self._crawled_ids)))
        )
        
        self._db_conn.commit()
    
    def load_progress(self, task_id: str) -> Set[str]:
        """
        加载已保存的进度
        
        Args:
            task_id: 任务标识（creator模式为user_id，search模式为keyword）
            
        Returns:
            已爬取的 note_id 集合（Set，O(1) 查找）
        """
        self._current_task_id = task_id
        self._start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # SQLite 模式
        if self.use_sqlite:
            db_file = self.get_db_file(task_id)
            if db_file.exists():
                self._init_sqlite(task_id)
                self._load_from_sqlite()
                utils.logger.info(f"[断点续爬] SQLite模式 - 已爬取: {len(self._crawled_ids)} 条")
                return self._crawled_ids.copy()
            else:
                self._init_sqlite(task_id)
                utils.logger.info(f"[断点续爬] SQLite模式 - 从头开始爬取")
                return set()
        
        # JSON 模式
        progress_file = self.get_progress_file(task_id)
        
        if not progress_file.exists():
            utils.logger.info(f"[断点续爬] 未找到进度文件，从头开始爬取: {task_id}")
            return set()
        
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 使用 Set 存储，查找复杂度 O(1)
            self._crawled_ids = set(data.get("crawled_ids", []))
            self._failed_ids = set(data.get("failed_ids", []))
            self._comment_crawled_ids = set(data.get("comment_crawled_ids", []))
            
            last_update = data.get("last_update", "未知")
            total_crawled = len(self._crawled_ids)
            total_failed = len(self._failed_ids)
            total_comments = len(self._comment_crawled_ids)
            
            utils.logger.info(f"[断点续爬] 加载进度成功: {task_id}")
            utils.logger.info(f"[断点续爬] 上次更新: {last_update}")
            utils.logger.info(f"[断点续爬] 作品: {total_crawled} 条, 失败: {total_failed} 条, 评论: {total_comments} 条")
            
            return self._crawled_ids.copy()
            
        except Exception as e:
            utils.logger.warning(f"[断点续爬] 加载进度文件失败: {e}")
            return set()
    
    def add_crawled(self, note_id: str):
        """
        添加已成功爬取的 note_id
        时间复杂度: O(1)
        """
        if note_id:
            self._crawled_ids.add(note_id)  # O(1) 哈希插入
            self._pending_crawled.add(note_id)  # 增量缓冲
            # 如果之前在失败列表中，移除
            self._failed_ids.discard(note_id)
            self._pending_failed.discard(note_id)
    
    def add_failed(self, note_id: str):
        """
        添加爬取失败的 note_id
        时间复杂度: O(1)
        """
        if note_id and note_id not in self._crawled_ids:  # O(1) 哈希查找
            self._failed_ids.add(note_id)
            self._pending_failed.add(note_id)
    
    def add_comment_crawled(self, note_id: str):
        """
        添加评论已爬取的 note_id
        时间复杂度: O(1)
        """
        if note_id:
            self._comment_crawled_ids.add(note_id)
            self._pending_comment_crawled.add(note_id)
    
    def is_comment_crawled(self, note_id: str) -> bool:
        """检查评论是否已爬取"""
        return note_id in self._comment_crawled_ids
    
    def get_comment_crawled_ids(self) -> Set[str]:
        """获取评论已爬取的 note_id 集合"""
        return self._comment_crawled_ids.copy()
    
    def is_crawled(self, note_id: str) -> bool:
        """检查是否已爬取"""
        return note_id in self._crawled_ids
    
    def save_progress(self):
        """
        保存当前进度
        - SQLite 模式: 增量保存，只写入新增的数据
        - JSON 模式: 全量保存
        """
        if not self._current_task_id:
            return
        
        # SQLite 模式：增量保存（更快）
        if self.use_sqlite and self._db_conn:
            try:
                self._save_to_sqlite_incremental()
                utils.logger.debug(f"[断点续爬] SQLite增量保存完成")
            except Exception as e:
                utils.logger.error(f"[断点续爬] SQLite保存失败: {e}")
            return
        
        # JSON 模式：全量保存
        progress_file = self.get_progress_file(self._current_task_id)
        
        try:
            data = {
                "task_id": self._current_task_id,
                "platform": self.platform,
                "crawler_type": self.crawler_type,
                "start_time": self._start_time,
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "crawled_count": len(self._crawled_ids),
                "failed_count": len(self._failed_ids),
                "comment_crawled_count": len(self._comment_crawled_ids),
                "crawled_ids": list(self._crawled_ids),
                "failed_ids": list(self._failed_ids),
                "comment_crawled_ids": list(self._comment_crawled_ids)
            }
            
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 清空增量缓冲
            self._pending_crawled.clear()
            self._pending_failed.clear()
            self._pending_comment_crawled.clear()
            
            utils.logger.info(f"[断点续爬] 进度已保存: 作品 {len(self._crawled_ids)}, 失败 {len(self._failed_ids)}, 评论 {len(self._comment_crawled_ids)}")
            
        except Exception as e:
            utils.logger.error(f"[断点续爬] 保存进度失败: {e}")
    
    def get_failed_ids(self) -> Set[str]:
        """获取失败的 note_id 列表（用于重试）"""
        return self._failed_ids.copy()
    
    def clear_progress(self, task_id: str):
        """清除指定任务的进度（用于重新爬取）"""
        progress_file = self.get_progress_file(task_id)
        if progress_file.exists():
            progress_file.unlink()
            utils.logger.info(f"[断点续爬] 已清除进度: {task_id}")
    
    def get_stats(self) -> dict:
        """获取当前进度统计"""
        return {
            "task_id": self._current_task_id,
            "crawled_count": len(self._crawled_ids),
            "failed_count": len(self._failed_ids),
            "start_time": self._start_time
        }


# 全局进度管理器实例
_progress_manager: Optional[CrawlProgressManager] = None


def get_progress_manager(platform: str = "xhs", crawler_type: str = "creator") -> CrawlProgressManager:
    """获取全局进度管理器"""
    global _progress_manager
    if _progress_manager is None or _progress_manager.platform != platform or _progress_manager.crawler_type != crawler_type:
        _progress_manager = CrawlProgressManager(platform, crawler_type)
    return _progress_manager


def reset_progress_manager():
    """重置全局进度管理器"""
    global _progress_manager
    _progress_manager = None
