# -*- coding: utf-8 -*-
"""
流水线飞书写入器
每完成一个作者立即写入飞书，每满 10 条视频触发并发脚本提取。
支持复用已有多维表格，增量追加新内容。
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Set

from tools import utils
from tools.feishu_bitable import FeishuBitableClient, map_note_to_feishu_record

CONFIG_PATH = os.path.join("config", "anti_crawl_config.json")


class PipelineFeishuWriter:
    """
    流水线写入器。

    用法::

        with PipelineFeishuWriter(app_id, app_secret) as writer:
            writer.write_creator("作者A", notes_a)
            writer.write_creator("作者B", notes_b)
            writer.finalize()
    """

    def __init__(
        self,
        feishu_app_id: str,
        feishu_app_secret: str,
        bitable_name: str = "",
        folder_token: str = "",
        reuse_app_token: str = "",
    ):
        self.feishu_app_id = feishu_app_id
        self.feishu_app_secret = feishu_app_secret
        self.client = FeishuBitableClient(feishu_app_id, feishu_app_secret)
        self.bitable_name = (
            bitable_name
            or f"小红书爬取数据_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        self.folder_token = folder_token
        self._reuse_app_token = reuse_app_token

        self._feishu_cfg: Dict = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self._feishu_cfg = json.load(f).get("feishu", {})
        except Exception:
            pass

        self.is_image_mode = self._feishu_cfg.get("image_mode", "link") == "image"
        self._image_threads = self._feishu_cfg.get("image_threads", 2)

        self.app_token: Optional[str] = None
        self.bitable_url: Optional[str] = None
        self.summary_table_id: Optional[str] = None
        self._first_creator = True
        self._total_inserted = 0
        self._total_skipped_existing = 0
        self._creator_count = 0
        self._video_serial = 0
        self._is_reusing = False

        self._existing_tables: Dict[str, str] = {}
        self._table_name_to_id: Dict[str, str] = {}

        self._all_field_names: Set[str] = set()
        self._ordered_fields: List[str] = []
        self._all_table_fields: List[Dict] = []
        self._attachment_fields: Set[str] = set()
        self._url_fields = {"链接"}
        self._date_fields = {"发布时间"}

        self._pending_videos: List[Dict] = []
        self._extraction_futures: list = []
        self._extraction_pool: Optional[ThreadPoolExecutor] = None
        self._script_extractor = None
        self._lock = threading.Lock()

        self.total_images_uploaded = 0
        self.total_images_failed = 0
        self.total_videos_uploaded = 0
        self.total_videos_skipped = 0

        self._write_executor: Optional[ThreadPoolExecutor] = None
        self._write_futures: List = []

    def _ensure_bitable(self):
        if self.app_token:
            return

        if self._reuse_app_token:
            try:
                tables = self.client.list_tables(self._reuse_app_token)
                self.app_token = self._reuse_app_token
                last_bi = self._feishu_cfg.get("last_bitable", {})
                self.bitable_url = last_bi.get(
                    "url",
                    f"https://guanghe.feishu.cn/base/{self._reuse_app_token}",
                )
                for t in tables:
                    name = t.get("name", "")
                    tid = t.get("table_id", "")
                    if name and tid:
                        self._table_name_to_id[name] = tid
                    if name == "视频汇总":
                        self.summary_table_id = tid
                        existing_records = self.client.list_all_records(
                            self.app_token, tid
                        )
                        self._video_serial = len(existing_records)

                self._is_reusing = True
                self._first_creator = False
                utils.logger.info(
                    f"[Pipeline] 复用已有多维表格: {self.bitable_url} "
                    f"({len(tables)} 个数据表)"
                )
                return
            except Exception as e:
                utils.logger.warning(
                    f"[Pipeline] 复用表格失败({e})，将创建新表格"
                )

        result = self.client.create_bitable(
            self.bitable_name, self.folder_token or None
        )
        self.app_token = result["app_token"]
        self.bitable_url = result["url"]
        utils.logger.info(f"[Pipeline] 创建多维表格: {self.bitable_name}")

    def _resolve_field_type(self, name: str) -> int:
        if name in self._url_fields:
            return 15
        if name in self._attachment_fields:
            return 17
        if name in self._date_fields:
            return 5
        return 1

    def _rebuild_field_defs(self):
        ordered = [
            "账号名称", "内容类型", "标题", "正文", "标签", "链接",
            "发布时间", "点赞数", "收藏数", "评论数", "互动量", "热门",
            "视频附件", "视频脚本",
        ]
        img_fields = sorted(
            [f for f in self._all_field_names if f.startswith("图片")],
            key=lambda x: int(x.replace("图片", "") or "0"),
        )
        ordered.extend(img_fields)
        for f in self._all_field_names:
            if f not in ordered:
                ordered.append(f)

        self._attachment_fields = {"视频附件"}
        if self.is_image_mode:
            self._attachment_fields.update(img_fields)

        self._ordered_fields = ordered
        self._all_table_fields = [
            {"field_name": fn, "type": self._resolve_field_type(fn)}
            for fn in ordered
        ]

    def _get_script_extractor(self):
        if self._script_extractor is not None:
            return self._script_extractor
        cfg = self._feishu_cfg
        if not cfg.get("video_script_enabled", False):
            return None
        api_key = cfg.get("video_script_api_key", "")
        if not api_key:
            return None
        from tools.video_script_extractor import VideoScriptExtractor
        self._script_extractor = VideoScriptExtractor(
            feishu_app_id=self.feishu_app_id,
            feishu_app_secret=self.feishu_app_secret,
            gemini_base_url=cfg.get(
                "video_script_gateway_url",
                "https://ops-ai-gateway.yc345.tv/v1",
            ),
            gemini_api_key=api_key,
            gemini_model=cfg.get("video_script_model", "gemini-3-pro-preview"),
            concurrency=int(cfg.get("video_script_concurrency", 3)),
        )
        return self._script_extractor

    # ==================== 去重辅助 ====================

    def _get_existing_note_ids(self, table_id: str) -> Set[str]:
        """从已有数据表中提取全部 note_id（通过链接字段解析）"""
        mapping = self._get_existing_note_map(table_id)
        return set(mapping.keys())

    def _get_existing_note_map(self, table_id: str) -> Dict[str, str]:
        """返回 {note_id: record_id} 映射，用于去重和更新"""
        try:
            records = self.client.list_all_records(self.app_token, table_id)
            note_map: Dict[str, str] = {}
            for rec in records:
                record_id = rec.get("record_id", "")
                link = rec.get("fields", {}).get("链接", "")
                url = ""
                if isinstance(link, dict):
                    url = link.get("link", "") or link.get("text", "")
                elif isinstance(link, str):
                    url = link
                elif isinstance(link, list):
                    for seg in link:
                        if isinstance(seg, dict) and seg.get("link"):
                            url = seg["link"]
                            break
                        elif isinstance(seg, dict) and seg.get("text"):
                            url = seg["text"]
                            break
                nid = ""
                if "/explore/" in url:
                    nid = url.split("/explore/")[1].split("?")[0].split("/")[0]
                elif "/discovery/item/" in url:
                    nid = url.split("/discovery/item/")[1].split("?")[0].split("/")[0]
                if nid and record_id:
                    note_map[nid] = record_id
            return note_map
        except Exception as e:
            utils.logger.warning(f"[Pipeline] 查询已有记录失败: {e}")
            return {}

    # ==================== 互动量增量更新 ====================

    def _update_interaction_stats(
        self,
        table_id: str,
        notes: List[Dict],
        note_to_record: Dict[str, str],
    ):
        """对已存在的笔记批量更新互动量字段"""
        def _safe_int(v):
            if v is None or v == "":
                return 0
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        update_records = []
        for note in notes:
            nid = note.get("note_id", "")
            record_id = note_to_record.get(nid)
            if not record_id:
                continue
            liked = _safe_int(note.get("liked_count", 0))
            collected = _safe_int(note.get("collected_count", 0))
            comment = _safe_int(note.get("comment_count", 0))
            interaction = liked + collected + comment
            is_hot = "🔥 热门" if interaction >= 50 else ""
            update_records.append({
                "record_id": record_id,
                "fields": {
                    "点赞数": str(liked),
                    "收藏数": str(collected),
                    "评论数": str(comment),
                    "互动量": str(interaction),
                    "热门": is_hot,
                },
            })

        if not update_records:
            return

        updated = self.client.batch_update_records(
            self.app_token, table_id, update_records,
        )
        utils.logger.info(
            f"[Pipeline] 互动量更新: {updated}/{len(update_records)} 条成功"
        )

    # ==================== 单作者写入 ====================

    def write_creator(self, creator_name: str, notes: List[Dict]):
        if not notes:
            return
        self._ensure_bitable()

        safe_name = creator_name[:100]
        existing_table_id = self._table_name_to_id.get(safe_name) if self._is_reusing else None
        seq_offset = 0

        if existing_table_id:
            note_to_record = self._get_existing_note_map(existing_table_id)
            existing_note_ids = set(note_to_record.keys())

            new_notes = []
            update_notes = []
            for n in notes:
                nid = n.get("note_id", "")
                if nid in existing_note_ids:
                    update_notes.append(n)
                else:
                    new_notes.append(n)

            # 批量更新已有笔记的互动量
            if update_notes:
                self._update_interaction_stats(
                    existing_table_id, update_notes, note_to_record,
                )

            self._total_skipped_existing += len(update_notes)
            if not new_notes:
                utils.logger.info(
                    f"[Pipeline] {safe_name}: 无新内容 "
                    f"({len(update_notes)} 条互动量已更新)"
                )
                self._creator_count += 1
                return
            seq_offset = len(existing_note_ids)
            utils.logger.info(
                f"[Pipeline] {safe_name}: {len(new_notes)} 条新内容 "
                f"(已有 {len(existing_note_ids)} 条，{len(update_notes)} 条互动量已更新)"
            )
            notes = new_notes

        records: List[Dict] = []
        for note in notes:
            record = map_note_to_feishu_record(creator_name, note)
            records.append(record)
            self._all_field_names.update(record.get("fields", {}).keys())

        if self.is_image_mode:
            self._upload_images_for_records(notes, records)
            self._upload_videos_for_records(notes, records)

        self._rebuild_field_defs()
        ordered_with_serial = set(self._ordered_fields) | {"序号"}

        if existing_table_id:
            table_id = existing_table_id
            for fn in self._ordered_fields:
                if fn == "序号":
                    continue
                try:
                    self.client.add_field(
                        self.app_token, table_id, fn,
                        self._resolve_field_type(fn),
                    )
                except Exception:
                    pass
        elif self._first_creator:
            tables = self.client.list_tables(self.app_token)
            if tables:
                table_id = tables[0]["table_id"]
                try:
                    self.client.rename_table(
                        self.app_token, table_id, safe_name
                    )
                except Exception:
                    pass
                for fn in self._ordered_fields:
                    if fn == "序号":
                        continue
                    try:
                        self.client.add_field(
                            self.app_token, table_id, fn,
                            self._resolve_field_type(fn),
                        )
                    except Exception:
                        pass
            else:
                table_id = self.client.create_table(
                    self.app_token, safe_name, self._all_table_fields
                )
            self._first_creator = False
        else:
            utils.logger.info(
                f"[Pipeline] 写入作者: {safe_name} ({len(records)} 条)"
            )
            table_id = self.client.create_table(
                self.app_token, safe_name, self._all_table_fields
            )

        if not existing_table_id:
            self.client.cleanup_default_fields_and_records(
                self.app_token, table_id, ordered_with_serial
            )

        self._table_name_to_id[safe_name] = table_id

        for i, rec in enumerate(records, seq_offset + 1):
            rec["fields"]["序号"] = str(i)

        inserted = self.client.batch_insert_records(
            self.app_token, table_id, records
        )
        self._total_inserted += inserted
        self._creator_count += 1

        if not existing_table_id:
            try:
                fields_list = self.client.list_fields(self.app_token, table_id)
                hot_fid = ""
                for f in fields_list:
                    if f and f.get("field_name") == "热门":
                        hot_fid = f.get("field_id", "")
                        break
                if hot_fid:
                    self.client.create_view(
                        self.app_token, table_id,
                        view_name="🔥 热门作品",
                        filter_conditions=[
                            {"field_id": hot_fid, "operator": "isNotEmpty"}
                        ],
                    )
            except Exception:
                pass

        video_records = []
        for rec in records:
            fields = rec.get("fields", {})
            has_video = (
                fields.get("内容类型") == "视频"
                or fields.get("视频附件")
            )
            if has_video:
                vr = {"fields": dict(fields)}
                vr["fields"]["账号名称"] = creator_name
                video_records.append(vr)

        if video_records:
            self._append_video_summary(video_records)

        utils.logger.info(
            f"[Pipeline] {safe_name}: {inserted} 条写入完成"
            + (f" (追加到已有表)" if existing_table_id else "")
            + (f", {len(video_records)} 条视频入汇总表"
               if video_records else "")
        )

    def write_creator_async(self, creator_name: str, notes: List[Dict]):
        """异步版本：提交到后台队列，不阻塞主爬虫线程"""
        if not notes:
            return
        self._ensure_bitable()
        if self._write_executor is None:
            self._write_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="feishu-writer"
            )
        future = self._write_executor.submit(
            self.write_creator, creator_name, list(notes)
        )
        self._write_futures.append(future)

    def _drain_write_queue(self):
        """等待所有后台写入完成（finalize 时调用）"""
        if not self._write_futures:
            return
        total = len(self._write_futures)
        utils.logger.info(f"[Pipeline] 等待 {total} 个写入任务完成...")
        for i, f in enumerate(self._write_futures, 1):
            try:
                f.result()
            except Exception as e:
                utils.logger.error(f"[Pipeline] 写入任务 {i} 异常: {e}")
        self._write_futures.clear()
        if self._write_executor:
            self._write_executor.shutdown(wait=False)
            self._write_executor = None

    # ==================== 图片/视频上传 ====================

    def _upload_images_for_records(
        self, notes: List[Dict], records: List[Dict]
    ):
        from tools.batch_crawler import (
            _parse_image_urls,
            _upload_note_images_to_feishu,
        )
        for note, record in zip(notes, records):
            note_id = note.get("note_id", "")
            if not note_id:
                continue
            image_urls = _parse_image_urls(note.get("image_list", ""))
            if not image_urls:
                continue
            image_tokens = _upload_note_images_to_feishu(
                self.client, self.app_token, note_id,
                image_urls, self._image_threads,
            )
            for fn, att in image_tokens.items():
                record["fields"][fn] = att
                self.total_images_uploaded += 1
            expected = len(image_urls)
            uploaded = len(image_tokens)
            if uploaded < expected:
                self.total_images_failed += expected - uploaded
            for i in range(1, len(image_urls) + 1):
                fn = f"图片{i}"
                if fn not in image_tokens and fn in record["fields"]:
                    del record["fields"][fn]

        for record in records:
            for key in list(record.get("fields", {}).keys()):
                if key.startswith("图片") and isinstance(
                    record["fields"][key], str
                ):
                    del record["fields"][key]

    def _upload_videos_for_records(
        self, notes: List[Dict], records: List[Dict]
    ):
        from tools.batch_crawler import _upload_note_video_to_feishu
        for note, record in zip(notes, records):
            note_id = note.get("note_id", "")
            video_url = note.get("video_url", "")
            note_type = note.get("type", "")
            if note_type != "video" and not video_url:
                continue
            if not note_id:
                continue
            attachment = _upload_note_video_to_feishu(
                self.client, self.app_token, note_id, video_url
            )
            if attachment:
                record["fields"]["视频附件"] = attachment
                self.total_videos_uploaded += 1
            else:
                if "视频附件" in record["fields"]:
                    del record["fields"]["视频附件"]
                self.total_videos_skipped += 1
            time.sleep(0.5)

        for record in records:
            flds = record.get("fields", {})
            if "视频附件" in flds and isinstance(flds["视频附件"], str):
                del flds["视频附件"]

    # ==================== 视频汇总 + 脚本流水线 ====================

    def _append_video_summary(self, video_records: List[Dict]):
        if not self.summary_table_id:
            self.summary_table_id = self.client.create_table(
                self.app_token, "视频汇总", self._all_table_fields
            )
            ordered_with_serial = set(self._ordered_fields) | {"序号"}
            self.client.cleanup_default_fields_and_records(
                self.app_token, self.summary_table_id, ordered_with_serial
            )
            try:
                self.client.add_field(
                    self.app_token, self.summary_table_id, "视频公网链接", 15
                )
            except Exception:
                pass

        for vr in video_records:
            self._video_serial += 1
            vr["fields"]["序号"] = str(self._video_serial)

        inserted = self.client.batch_insert_records_full(
            self.app_token, self.summary_table_id, video_records
        )

        for rec in inserted:
            record_id = rec.get("record_id", "")
            fields = rec.get("fields", {})
            video_attach = fields.get("视频附件")
            if not video_attach or not isinstance(video_attach, list):
                continue
            ft = video_attach[0].get("file_token", "")
            if not ft or not record_id:
                continue

            title_raw = fields.get("标题", "")
            if isinstance(title_raw, list):
                title = "".join(
                    seg.get("text", "") if isinstance(seg, dict)
                    else str(seg)
                    for seg in title_raw
                )
            else:
                title = str(title_raw) if title_raw else ""

            account_raw = fields.get("账号名称", "")
            account = str(account_raw) if account_raw else ""

            self._pending_videos.append({
                "record_id": record_id,
                "file_token": ft,
                "title": title,
                "account": account,
            })

        while len(self._pending_videos) >= 10:
            batch = self._pending_videos[:10]
            self._pending_videos = self._pending_videos[10:]
            self._trigger_extraction(batch)

    def _trigger_extraction(self, batch: List[Dict]):
        extractor = self._get_script_extractor()
        if extractor is None:
            return
        if self._extraction_pool is None:
            concurrency = int(
                self._feishu_cfg.get("video_script_concurrency", 3)
            )
            self._extraction_pool = ThreadPoolExecutor(
                max_workers=concurrency
            )

        file_tokens = [item["file_token"] for item in batch]
        try:
            token_url_map = self.client.batch_get_tmp_download_url(
                file_tokens
            )
        except Exception as e:
            utils.logger.warning(f"[Pipeline] 获取下载链接失败: {e}")
            self._pending_videos.extend(batch)
            return

        for item in batch:
            video_url = token_url_map.get(item["file_token"], "")
            if not video_url:
                utils.logger.warning(
                    f"[Pipeline] 无下载链接: {item['title'][:50]}"
                )
                continue
            future = self._extraction_pool.submit(
                self._extract_one,
                item["record_id"], video_url,
                item["title"], item.get("account", ""),
            )
            self._extraction_futures.append(future)

    def _extract_one(
        self, record_id: str, video_url: str,
        title: str, account: str,
    ):
        from tools.video_script_extractor import SCRIPT_FIELD_NAME
        extractor = self._get_script_extractor()
        if not extractor:
            return

        utils.logger.info(f"[Pipeline] 提取脚本: {title[:50]}")
        script = extractor._call_gemini(
            video_url, title=title, account=account
        )
        if script.startswith("[提取失败]"):
            utils.logger.warning(
                f"[Pipeline] 脚本失败: {title[:50]}"
            )
        else:
            utils.logger.info(
                f"[Pipeline] 脚本完成: {title[:50]} ({len(script)} 字)"
            )
        self.client.batch_update_records(
            self.app_token, self.summary_table_id,
            [{"record_id": record_id,
              "fields": {SCRIPT_FIELD_NAME: script}}],
        )

    # ==================== 收尾 ====================

    def finalize(self):
        self._drain_write_queue()

        if self._pending_videos:
            self._trigger_extraction(self._pending_videos)
            self._pending_videos = []

        if self._extraction_futures:
            total_f = len(self._extraction_futures)
            utils.logger.info(
                f"[Pipeline] 等待 {total_f} 个脚本提取任务完成..."
            )
            done = 0
            for f in as_completed(self._extraction_futures):
                done += 1
                try:
                    f.result()
                except Exception as e:
                    utils.logger.error(
                        f"[Pipeline] 提取线程异常: {e}"
                    )
                if done % 10 == 0 or done == total_f:
                    utils.logger.info(
                        f"[Pipeline] 提取进度: {done}/{total_f}"
                    )
            self._extraction_futures.clear()

        if self._extraction_pool:
            self._extraction_pool.shutdown(wait=True)
            self._extraction_pool = None

        if self.summary_table_id:
            try:
                sf = self.client.list_fields(
                    self.app_token, self.summary_table_id
                )
                hot_fid = ""
                for f in sf:
                    if f and f.get("field_name") == "热门":
                        hot_fid = f.get("field_id", "")
                        break
                if hot_fid:
                    self.client.create_view(
                        self.app_token, self.summary_table_id,
                        view_name="🔥 热门视频",
                        filter_conditions=[{
                            "field_id": hot_fid,
                            "operator": "isNotEmpty",
                        }],
                    )
            except Exception:
                pass

            try:
                self._write_public_video_urls()
            except Exception as e:
                utils.logger.warning(
                    f"[Pipeline] 公网链接写入失败: {e}"
                )

            try:
                self._final_script_sweep()
            except Exception as e:
                utils.logger.warning(
                    f"[Pipeline] 最终脚本校验失败: {e}"
                )

        self._save_bitable_to_config()

        msg = (
            f"[Pipeline] 全部完成: {self._total_inserted} 条新写入, "
            f"{self._creator_count} 个作者"
        )
        if self._total_skipped_existing > 0:
            msg += f", {self._total_skipped_existing} 条已存在(互动量已更新)"
        msg += f", URL: {self.bitable_url}"
        utils.logger.info(msg)

    def _save_bitable_to_config(self):
        """将当前多维表格信息回写到配置文件，供下次增量复用"""
        if not self.app_token:
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            feishu = cfg.setdefault("feishu", {})
            feishu["last_bitable"] = {
                "app_token": self.app_token,
                "url": self.bitable_url or "",
                "tables": {
                    name: tid
                    for name, tid in self._table_name_to_id.items()
                    if name != "视频汇总"
                },
                "summary_table_id": self.summary_table_id or "",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            utils.logger.info(
                f"[Pipeline] 多维表格信息已回写配置 "
                f"(app_token={self.app_token}, "
                f"{len(self._table_name_to_id)} 个数据表)"
            )
        except Exception as e:
            utils.logger.warning(f"[Pipeline] 回写配置失败: {e}")

    def _write_public_video_urls(self):
        records = self.client.list_all_records(
            self.app_token, self.summary_table_id
        )
        record_file_map: Dict[str, str] = {}
        all_tokens: List[str] = []
        for rec in records:
            rid = rec.get("record_id", "")
            va = rec.get("fields", {}).get("视频附件")
            if va and isinstance(va, list):
                ft = va[0].get("file_token", "")
                if ft:
                    record_file_map[rid] = ft
                    all_tokens.append(ft)
        if not all_tokens:
            return
        url_map = self.client.batch_get_tmp_download_url(all_tokens)
        updates = []
        for rid, ft in record_file_map.items():
            url = url_map.get(ft, "")
            if url:
                updates.append({
                    "record_id": rid,
                    "fields": {
                        "视频公网链接": {"link": url, "text": url}
                    },
                })
        if updates:
            self.client.batch_update_records(
                self.app_token, self.summary_table_id, updates
            )
            utils.logger.info(
                f"[Pipeline] 视频公网链接: {len(updates)} 条"
            )

    def _final_script_sweep(self):
        extractor = self._get_script_extractor()
        if extractor is None:
            utils.logger.info(
                "[Pipeline] 脚本提取未启用，跳过最终校验"
            )
            return
        utils.logger.info(
            "[Pipeline] 最终校验: 扫描所有视频确保脚本完整..."
        )
        result = extractor.extract_and_write(
            app_token=self.app_token,
            table_id=self.summary_table_id,
            skip_existing=True,
        )
        proc = result.get("processed", 0)
        fail = result.get("failed", 0)
        if proc > 0 or fail > 0:
            utils.logger.info(
                f"[Pipeline] 最终校验: 补充 {proc}, 失败 {fail}"
            )
        else:
            utils.logger.info(
                "[Pipeline] 最终校验通过: 所有视频已有脚本"
            )

    def cleanup_local_media(self):
        """删除本地已上传的图片和视频缓存（仅在数据已确认写入飞书后调用）"""
        import shutil
        dirs_to_clean = ["data/xhs/images", "data/xhs/videos"]
        total_freed = 0
        for d in dirs_to_clean:
            if not os.path.exists(d):
                continue
            try:
                count = sum(1 for _ in os.scandir(d) if _.is_dir())
                shutil.rmtree(d)
                os.makedirs(d, exist_ok=True)
                total_freed += count
                utils.logger.info(
                    f"[Pipeline] 清理本地缓存: {d} ({count} 个目录)"
                )
            except Exception as e:
                utils.logger.warning(f"[Pipeline] 清理 {d} 失败: {e}")
        if total_freed > 0:
            utils.logger.info(
                f"[Pipeline] 本地媒体清理完成: 共 {total_freed} 个笔记目录"
            )

    def close(self):
        if self._script_extractor:
            self._script_extractor.close()
            self._script_extractor = None
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
