# -*- coding: utf-8 -*-
"""
视频脚本提取器
从飞书视频汇总表读取视频，调用 Gemini API 提取口播脚本，回写到飞书
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Callable

import httpx

from tools import utils
from tools.feishu_bitable import FeishuBitableClient


# ==================== 默认配置 ====================

DEFAULT_PROMPT = """# 角色
你是一位专业的短视频脚本拆解专家。你的任务是根据视频的口播内容和元信息，还原出一份完整的、可复现的视频拍摄脚本。

# 输入信息
- 视频标题：{{标题}}
- 账号名称：{{账号名称}}
- 口播原文：{{语音转文字结果}}

# 任务要求
1. 严格基于口播内容进行拆解，不得臆想或添加视频中不存在的内容
2. 按时间顺序逐段拆解，标注每段的时间节点
3. 每段需包含：画面描述、口播文案、字幕/贴纸（如有提及）、BGM/音效风格
4. 识别视频结构（开头钩子→正文→转折→结尾引导）

# 输出格式

## 基本信息
- 视频类型：（口播/剧情/教程/展示/混剪）
- 预估总时长：
- 核心主题：（不超过3个关键词）

## 脚本拆解

| 时间段 | 画面描述 | 口播文案 | 字幕/贴纸 | 备注 |
|--------|---------|---------|----------|------|
| 00:00-00:03 | 真人出镜，面对镜头 | "家长们注意了！" | 大字标题弹出 | 开头钩子 |
| 00:03-00:15 | 切换到课程画面 | "很多家长问我..." | 重点文字高亮 | 痛点引入 |
| ... | ... | ... | ... | ... |

## 结构分析
- 开头钩子（0-3秒）：用了什么方式抓注意力
- 正文展开：核心卖点如何呈现
- 结尾引导：如何引导互动/转化

# 限制
1. 不做主观评价，只做客观拆解
2. 不添加视频中未出现的内容
3. 口播文案必须忠实于原文，可适当断句但不改动用词"""

SCRIPT_FIELD_NAME = "视频脚本"
SCRIPT_FIELD_TYPE = 1  # 多行文本


class VideoScriptExtractor:
    """视频脚本提取器"""

    def __init__(
        self,
        feishu_app_id: str,
        feishu_app_secret: str,
        gemini_base_url: str,
        gemini_api_key: str,
        gemini_model: str = "gemini-3-pro-preview",
        prompt: str = DEFAULT_PROMPT,
        max_tokens: int = 8192,
        request_timeout: float = 600.0,
        retry_count: int = 3,
        interval_sec: float = 2.0,
        concurrency: int = 5,
    ):
        self.feishu_client = FeishuBitableClient(feishu_app_id, feishu_app_secret)
        self.gemini_base_url = gemini_base_url.rstrip("/")
        self.gemini_api_key = gemini_api_key
        self.gemini_model = gemini_model
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.request_timeout = request_timeout
        self.retry_count = retry_count
        self.interval_sec = interval_sec
        self.concurrency = concurrency
        self._http = httpx.Client(timeout=request_timeout)

    def close(self):
        self._http.close()
        self.feishu_client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ==================== Gemini 调用 ====================

    @staticmethod
    def _extract_text_field(field_value) -> str:
        """从飞书字段值中提取纯文本"""
        if isinstance(field_value, list):
            parts = []
            for seg in field_value:
                if isinstance(seg, dict):
                    parts.append(seg.get("text", ""))
                elif isinstance(seg, str):
                    parts.append(seg)
            return "".join(parts)
        elif isinstance(field_value, str):
            return field_value
        return ""

    def _render_prompt(self, title: str = "", account: str = "",
                       transcript: str = "") -> str:
        """渲染提示词模板，替换 {{变量}}"""
        text = self.prompt
        text = text.replace("{{标题}}", title)
        text = text.replace("{{账号名称}}", account)
        # 如果 prompt 中有语音转文字占位符，提示模型自行提取
        if "{{语音转文字结果}}" in text:
            if transcript:
                text = text.replace("{{语音转文字结果}}", transcript)
            else:
                text = text.replace(
                    "{{语音转文字结果}}",
                    "（请直接从视频中识别并提取口播原文）"
                )
        return text

    @staticmethod
    def _parse_sse_content(response: httpx.Response) -> str:
        """从 SSE 流式响应中解析并拼接完整文本"""
        parts: list[str] = []
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                text = delta.get("content", "")
                if text:
                    parts.append(text)
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
        return "".join(parts)

    def _call_gemini(self, video_url: str, title: str = "",
                     account: str = "") -> str:
        """传递视频 URL 给模型，流式接收结果（防止网关 504）"""
        rendered_prompt = self._render_prompt(title=title, account=account)
        body = {
            "model": self.gemini_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": rendered_prompt},
                    {"type": "image_url", "image_url": {"url": video_url}},
                ],
            }],
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.gemini_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.gemini_base_url}/chat/completions"

        last_err = None
        for attempt in range(1, self.retry_count + 1):
            try:
                with self._http.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code != 200:
                        err_body = resp.read().decode(errors="replace")[:500]
                        last_err = f"HTTP {resp.status_code}: {err_body}"
                        utils.logger.warning(
                            f"[ScriptExtractor] Gemini 请求失败 "
                            f"(尝试 {attempt}/{self.retry_count}): "
                            f"HTTP {resp.status_code}"
                        )
                        if attempt < self.retry_count:
                            backoff = min(15 * (2 ** (attempt - 1)), 120)
                            utils.logger.info(
                                f"[ScriptExtractor] 等待 {backoff}s 后重试..."
                            )
                            time.sleep(backoff)
                        continue

                    content = self._parse_sse_content(resp)
                    if content.strip():
                        return content.strip()
                    last_err = "流式响应未返回有效文本"
                    utils.logger.warning(
                        f"[ScriptExtractor] Gemini 响应无内容 (尝试 {attempt})"
                    )
            except Exception as e:
                last_err = str(e)
                utils.logger.warning(
                    f"[ScriptExtractor] Gemini 调用异常 "
                    f"(尝试 {attempt}/{self.retry_count}): {e}"
                )
            if attempt < self.retry_count:
                backoff = min(15 * (2 ** (attempt - 1)), 120)
                time.sleep(backoff)

        return f"[提取失败] {last_err}"

    # ==================== 主流程 ====================

    def extract_and_write(
        self,
        app_token: str,
        table_id: str = "",
        skip_existing: bool = True,
        hot_only: bool = True,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict:
        """
        从飞书汇总表提取视频脚本并回写

        Args:
            app_token: 多维表格 token
            table_id: 视频汇总表 ID（空则自动查找）
            skip_existing: 跳过已有脚本的记录
            hot_only: True=仅处理热门视频, False=处理全部视频
            on_progress: 进度回调 (current, total, title)

        Returns:
            {"success": bool, "total": int, "processed": int,
             "skipped": int, "failed": int, "message": str}
        """
        t0 = time.time()

        # 1. 查找视频汇总表
        if not table_id:
            tables = self.feishu_client.list_tables(app_token)
            for t in tables:
                if "视频汇总" in t.get("name", ""):
                    table_id = t["table_id"]
                    break
            if not table_id:
                return {
                    "success": False, "message": "未找到「视频汇总」数据表",
                    "total": 0, "processed": 0, "skipped": 0, "failed": 0,
                }

        utils.logger.info(f"[ScriptExtractor] 目标表: {table_id}")

        # 2. 确保"视频脚本"字段存在
        try:
            self.feishu_client.add_field(
                app_token, table_id, SCRIPT_FIELD_NAME, SCRIPT_FIELD_TYPE
            )
            utils.logger.info(f"[ScriptExtractor] 已添加字段: {SCRIPT_FIELD_NAME}")
        except Exception:
            pass  # 字段已存在

        # 3. 读取所有记录
        records = self.feishu_client.list_all_records(app_token, table_id)
        utils.logger.info(f"[ScriptExtractor] 读取到 {len(records)} 条记录")

        # 4. 筛选需要处理的记录
        to_process: List[Dict] = []
        skipped = 0
        skipped_not_hot = 0
        scope_label = "仅热门" if hot_only else "全部"
        utils.logger.info(f"[ScriptExtractor] 提取范围: {scope_label}")

        for rec in records:
            fields = rec.get("fields", {})
            video_attach = fields.get("视频附件")
            if not video_attach or not isinstance(video_attach, list):
                continue

            ft = video_attach[0].get("file_token", "")
            if not ft:
                continue

            # 热门过滤（hot_only=True 时仅处理热门视频）
            if hot_only:
                hot_val = self._extract_text_field(
                    fields.get("热门", "")
                ).strip()
                if not hot_val:
                    skipped_not_hot += 1
                    continue

            # 检查是否已有脚本（失败记录不跳过，允许重试）
            if skip_existing:
                existing = fields.get(SCRIPT_FIELD_NAME, "")
                existing_text = ""
                if isinstance(existing, str):
                    existing_text = existing.strip()
                elif isinstance(existing, list) and existing:
                    existing_text = self._extract_text_field(existing).strip()
                if existing_text and not existing_text.startswith("[提取失败]"):
                    skipped += 1
                    continue

            # 提取标题和账号名称
            title = self._extract_text_field(fields.get("标题", ""))
            account = self._extract_text_field(fields.get("账号名称", ""))

            to_process.append({
                "record_id": rec["record_id"],
                "file_token": ft,
                "title": title,
                "account": account,
            })

        if skipped_not_hot > 0:
            utils.logger.info(
                f"[ScriptExtractor] 跳过 {skipped_not_hot} 条非热门视频"
            )

        total = len(to_process)
        utils.logger.info(
            f"[ScriptExtractor] 待处理: {total}, 已跳过: {skipped}"
        )

        if total == 0:
            elapsed = round(time.time() - t0, 1)
            return {
                "success": True,
                "message": f"无需处理（已跳过 {skipped} 条已有脚本的记录）",
                "total": len(records), "processed": 0,
                "skipped": skipped, "failed": 0,
                "elapsed_sec": elapsed,
            }

        # 5. 批量获取临时下载链接
        all_tokens = [p["file_token"] for p in to_process]
        token_url_map = self.feishu_client.batch_get_tmp_download_url(all_tokens)

        # 6. 并发处理
        processed = 0
        failed = 0
        counter_lock = threading.Lock()
        write_lock = threading.Lock()
        update_batch: List[Dict] = []
        finished_count = 0

        failed_items: List[Dict] = []

        def _flush_batch(force: bool = False):
            nonlocal update_batch
            batch_to_write = []
            with write_lock:
                if len(update_batch) >= 10 or (force and update_batch):
                    batch_to_write = update_batch[:]
                    update_batch = []
            if batch_to_write:
                try:
                    self.feishu_client.batch_update_records(
                        app_token, table_id, batch_to_write
                    )
                except Exception as e:
                    utils.logger.warning(f"[ScriptExtractor] 批量写入失败: {e}")

        def _process_one(idx: int, item: Dict) -> None:
            nonlocal processed, failed, finished_count
            record_id = item["record_id"]
            file_token = item["file_token"]
            title = item["title"]
            account = item.get("account", "")

            video_url = token_url_map.get(file_token, "")
            if not video_url:
                utils.logger.warning(
                    f"[ScriptExtractor] [{idx+1}/{total}] 无下载链接: {title[:50]}"
                )
                with counter_lock:
                    failed += 1
                    finished_count += 1
                return

            utils.logger.info(
                f"[ScriptExtractor] [{idx+1}/{total}] 处理: {title[:50]}"
            )

            script = self._call_gemini(video_url, title=title, account=account)

            with counter_lock:
                finished_count += 1
                if script.startswith("[提取失败]"):
                    utils.logger.error(
                        f"[ScriptExtractor] [{idx+1}/{total}] 失败: {title[:50]} -> {script[:100]}"
                    )
                    failed += 1
                    failed_items.append(item)
                else:
                    utils.logger.info(
                        f"[ScriptExtractor] [{idx+1}/{total}] 完成: {title[:50]} "
                        f"({len(script)} 字) [进度 {finished_count}/{total}]"
                    )
                    processed += 1

            if on_progress:
                on_progress(finished_count, total, title[:50])

            with write_lock:
                update_batch.append({
                    "record_id": record_id,
                    "fields": {SCRIPT_FIELD_NAME: script},
                })
                should_flush = len(update_batch) >= 10
            if should_flush:
                _flush_batch()

        workers = min(self.concurrency, total)
        utils.logger.info(
            f"[ScriptExtractor] 启动 {workers} 并发处理 {total} 条视频..."
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_process_one, i, item): i
                for i, item in enumerate(to_process)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    utils.logger.error(f"[ScriptExtractor] 线程异常: {e}")
                    with counter_lock:
                        failed += 1
                        finished_count += 1

        _flush_batch(force=True)

        # 对失败项自动重试一轮（串行，避免压垮网关）
        if failed_items:
            retry_count = len(failed_items)
            utils.logger.info(
                f"[ScriptExtractor] 开始重试 {retry_count} 条失败记录..."
            )
            retry_ok = 0
            for ri, item in enumerate(failed_items):
                record_id = item["record_id"]
                title = item["title"]
                account = item.get("account", "")
                video_url = token_url_map.get(item["file_token"], "")
                if not video_url:
                    continue

                utils.logger.info(
                    f"[ScriptExtractor] 重试 [{ri+1}/{retry_count}]: {title[:50]}"
                )
                time.sleep(5)
                script = self._call_gemini(video_url, title=title, account=account)

                if not script.startswith("[提取失败]"):
                    utils.logger.info(
                        f"[ScriptExtractor] 重试成功: {title[:50]} ({len(script)} 字)"
                    )
                    retry_ok += 1
                    failed -= 1
                    processed += 1

                self.feishu_client.batch_update_records(
                    app_token, table_id,
                    [{"record_id": record_id, "fields": {SCRIPT_FIELD_NAME: script}}],
                )

            utils.logger.info(
                f"[ScriptExtractor] 重试完成: {retry_ok}/{retry_count} 条成功"
            )

        elapsed = round(time.time() - t0, 1)
        msg = (
            f"脚本提取完成: 成功 {processed}, 失败 {failed}, "
            f"跳过 {skipped}, 耗时 {elapsed}s"
        )
        utils.logger.info(f"[ScriptExtractor] {msg}")

        return {
            "success": True,
            "message": msg,
            "total": len(records),
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "elapsed_sec": elapsed,
            "table_id": table_id,
        }
