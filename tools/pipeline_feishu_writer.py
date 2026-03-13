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
from tools.feishu_bitable import (
    FeishuBitableClient,
    map_note_to_feishu_record,
    map_search_note_to_feishu_record,
    map_comment_to_feishu_record,
    build_comment_summary,
    sort_comments_first_level_then_replies,
    DEFAULT_FIELD_NAMES_NOTE,
    DEFAULT_FIELD_NAMES_SEARCH_NOTE,
    DEFAULT_FIELD_NAMES_COMMENT,
    FIELD_TYPE_TEXT,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_DATE,
    FIELD_TYPE_SELECT,
    FIELD_TYPE_URL,
    FIELD_TYPE_ATTACHMENT,
)

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

        # 飞书字段名配置（config.feishu.field_names），不同任务可改配置无需改代码
        _raw = self._feishu_cfg.get("field_names", {}) or {}
        self._fn_note = {**DEFAULT_FIELD_NAMES_NOTE, **(_raw.get("note") or {})}
        self._fn_search_note = {**DEFAULT_FIELD_NAMES_SEARCH_NOTE, **(_raw.get("search_note") or {})}
        self._fn_comment = {**DEFAULT_FIELD_NAMES_COMMENT, **(_raw.get("comment") or {})}

        self.is_image_mode = self._feishu_cfg.get("image_mode", "link") == "image"
        self._image_threads = self._feishu_cfg.get("image_threads", 4)

        self.app_token: Optional[str] = None
        self.bitable_url: Optional[str] = None
        self.summary_table_id: Optional[str] = None
        self.image_text_summary_table_id: Optional[str] = None
        self._first_creator = True
        self._total_inserted = 0
        self._total_skipped_existing = 0
        self._creator_count = 0
        self._video_serial = 0
        self._image_text_serial = 0
        self._is_reusing = False
        self._video_summary_note_map: Dict[str, str] = {}
        self._image_text_summary_note_map: Dict[str, str] = {}
        self._summary_lock = threading.Lock()

        self._owner_open_id: str = (self._feishu_cfg.get("owner_open_id") or "").strip()
        self._transfer_owner: bool = self._feishu_cfg.get("transfer_owner", True) is not False
        # 任务级覆盖（CLI --transfer_owner / --owner_open_id / --collaborator_*）
        import config as _cfg
        if hasattr(_cfg, "TRANSFER_OWNER_OVERRIDE"):
            self._transfer_owner = _cfg.TRANSFER_OWNER_OVERRIDE
        if hasattr(_cfg, "OWNER_OPEN_ID_OVERRIDE") and _cfg.OWNER_OPEN_ID_OVERRIDE:
            self._owner_open_id = _cfg.OWNER_OPEN_ID_OVERRIDE
        self._collaborator_open_ids_override = getattr(_cfg, "COLLABORATOR_OPEN_IDS_OVERRIDE", None)
        self._collaborator_user_ids_override = getattr(_cfg, "COLLABORATOR_USER_IDS_OVERRIDE", None)

        self._existing_tables: Dict[str, str] = {}
        self._table_name_to_id: Dict[str, str] = {}

        self._all_field_names: Set[str] = set()
        self._ordered_fields: List[str] = []
        self._all_table_fields: List[Dict] = []
        self._attachment_fields: Set[str] = set()
        self._url_fields = {self._fn_note.get("link", "链接")}
        self._date_fields = {self._fn_note.get("time", "发布时间")}

        self._pending_videos: List[Dict] = []
        self._extraction_futures: list = []
        self._extraction_pool: Optional[ThreadPoolExecutor] = None
        self._script_extractor = None
        self._script_hot_only = self._feishu_cfg.get(
            "video_script_scope", "hot"
        ) == "hot"
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
                        self._video_summary_note_map = self._build_note_map_from_records(existing_records)
                    elif name == "图文汇总":
                        self.image_text_summary_table_id = tid
                        existing_records = self.client.list_all_records(
                            self.app_token, tid
                        )
                        self._image_text_serial = len(existing_records)
                        self._image_text_summary_note_map = self._build_note_map_from_records(existing_records)

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
        self._add_bitable_collaborators(self.app_token)

    def _add_bitable_collaborators(self, app_token: str) -> None:
        """创建表格后，将配置中的协作者加入多维表格，便于查看/编辑"""
        open_ids = self._collaborator_open_ids_override if self._collaborator_open_ids_override is not None else (self._feishu_cfg.get("collaborator_open_ids") or [])
        if not isinstance(open_ids, list):
            open_ids = [open_ids] if open_ids else []
        for open_id in open_ids:
            if not open_id or not str(open_id).strip():
                continue
            self.client.add_permission_member(
                token=app_token,
                member_type="openid",
                member_id=str(open_id).strip(),
                perm="edit",
                node_type="bitable",
            )
        user_ids = self._collaborator_user_ids_override if self._collaborator_user_ids_override is not None else (self._feishu_cfg.get("collaborator_user_ids") or [])
        if not isinstance(user_ids, list):
            user_ids = [user_ids] if user_ids else []
        for uid in user_ids:
            if not uid or not str(uid).strip():
                continue
            self.client.add_permission_member(
                token=app_token,
                member_type="userid",
                member_id=str(uid).strip(),
                perm="edit",
                node_type="bitable",
            )

    def _resolve_field_type(self, name: str) -> int:
        if name in self._url_fields:
            return 15
        if name in self._attachment_fields:
            return 17
        if name in self._date_fields:
            return 5
        return 1

    def _rebuild_field_defs(self):
        # 顺序来自配置 field_names.note（固定逻辑键顺序）
        logical_order = [
            "creator_name", "content_type", "title", "desc", "tag_list", "link",
            "time", "liked_count", "collected_count", "comment_count", "interaction", "hot",
            "video_attachment", "video_script", "seq",
        ]
        ordered = [self._fn_note.get(k, DEFAULT_FIELD_NAMES_NOTE.get(k, k)) for k in logical_order]
        img_fields = sorted(
            [f for f in self._all_field_names if f.startswith("图片")],
            key=lambda x: int(x.replace("图片", "") or "0"),
        )
        ordered.extend(img_fields)
        for f in self._all_field_names:
            if f not in ordered:
                ordered.append(f)

        self._attachment_fields = {self._fn_note.get("video_attachment", "视频附件")}
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

    def _ensure_table_fields(self, table_id: str, records: List[Dict]):
        """检查 records 中的字段是否都存在于表中，缺失的自动添加"""
        all_rec_fields: Set[str] = set()
        for rec in records:
            all_rec_fields.update(rec.get("fields", {}).keys())
        if not all_rec_fields:
            return
        try:
            existing = self.client.list_fields(self.app_token, table_id)
            existing_names = {f.get("field_name", "") for f in existing if f}
        except Exception as e:
            utils.logger.warning(f"[Feishu] 获取表字段列表失败: {e}")
            return
        missing = all_rec_fields - existing_names
        if not missing:
            return
        added = 0
        for fn in sorted(missing):
            try:
                self.client.add_field(
                    self.app_token, table_id, fn, self._resolve_field_type(fn),
                )
                added += 1
            except Exception:
                pass
        if added:
            utils.logger.info(f"[Feishu] 自动补齐 {added} 个缺失字段: {sorted(missing)}")

    def _create_summary_table(self, table_name: str) -> str:
        """创建汇总表，先尝试带字段创建，失败则回退到逐个添加字段"""
        table_id = ""
        try:
            table_id = self.client.create_table(
                self.app_token, table_name, self._all_table_fields
            )
        except Exception as e:
            utils.logger.warning(
                f"[Pipeline] 带字段创建 {table_name} 失败({e})，回退到逐个添加"
            )

        if not table_id:
            utils.logger.warning(
                f"[Pipeline] {table_name} table_id 为空，回退到逐个添加字段"
            )
            table_id = self.client.create_table(
                self.app_token, table_name, []
            )

        if not table_id:
            utils.logger.error(f"[Pipeline] 无法创建 {table_name}，跳过")
            return ""

        note_seq = self._fn_note.get("seq", "序号")
        ordered_with_serial = set(self._ordered_fields) | {note_seq}
        self.client.cleanup_default_fields_and_records(
            self.app_token, table_id, ordered_with_serial
        )

        for fn in self._ordered_fields:
            if fn == note_seq:
                continue
            try:
                self.client.add_field(
                    self.app_token, table_id, fn,
                    self._resolve_field_type(fn),
                )
            except Exception:
                pass
        return table_id

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
            link_field = self._fn_note.get("link", "链接")
            for rec in records:
                record_id = rec.get("record_id", "")
                link = rec.get("fields", {}).get(link_field, "")
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

    def _build_note_map_from_records(self, records: list) -> Dict[str, str]:
        """从已加载的记录列表构建 {note_id: record_id} 映射"""
        note_map: Dict[str, str] = {}
        link_field = self._fn_note.get("link", "链接")
        for rec in records:
            record_id = rec.get("record_id", "")
            link = rec.get("fields", {}).get(link_field, "")
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

    def _extract_note_id_from_record(self, rec: Dict) -> str:
        """从待写入的飞书记录中提取 note_id"""
        link_field = self._fn_note.get("link", "链接")
        link = rec.get("fields", {}).get(link_field, "")
        url = ""
        if isinstance(link, dict):
            url = link.get("link", "") or link.get("text", "")
        elif isinstance(link, str):
            url = link
        if "/explore/" in url:
            return url.split("/explore/")[1].split("?")[0].split("/")[0]
        elif "/discovery/item/" in url:
            return url.split("/discovery/item/")[1].split("?")[0].split("/")[0]
        return ""

    def _dedup_and_update_summary(
        self, records: List[Dict], note_map: Dict[str, str], table_id: str,
    ) -> List[Dict]:
        """去重汇总表记录：已存在的更新互动量，返回仅需新插入的记录"""
        if not note_map:
            utils.logger.info(f"[Feishu] 汇总表去重: 无已有记录映射，全部为新记录 ({len(records)} 条)")
            return records
        utils.logger.info(f"[Feishu] 汇总表去重: 对比 {len(records)} 条记录 (已有映射 {len(note_map)} 条)")

        new_records = []
        update_records = []
        fn = self._fn_note
        for rec in records:
            nid = self._extract_note_id_from_record(rec)
            existing_rid = note_map.get(nid) if nid else None
            if existing_rid:
                fields = rec.get("fields", {})
                update_records.append({
                    "record_id": existing_rid,
                    "fields": {
                        fn.get("liked_count", "点赞数"): fields.get(fn.get("liked_count", "点赞数"), ""),
                        fn.get("collected_count", "收藏数"): fields.get(fn.get("collected_count", "收藏数"), ""),
                        fn.get("comment_count", "评论数"): fields.get(fn.get("comment_count", "评论数"), ""),
                        fn.get("interaction", "互动量"): fields.get(fn.get("interaction", "互动量"), ""),
                        fn.get("hot", "热门"): fields.get(fn.get("hot", "热门"), ""),
                    },
                })
            else:
                new_records.append(rec)
                if nid:
                    note_map[nid] = "__pending__"

        if update_records:
            updated = self.client.batch_update_records(
                self.app_token, table_id, update_records,
            )
            utils.logger.info(
                f"[Pipeline] 汇总表互动量更新: {updated}/{len(update_records)} 条"
            )

        return new_records

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
            # 飞书多维表格中若为文本类型，数值须转为字符串，避免 TextFieldConvFail
            fn = self._fn_note
            update_records.append({
                "record_id": record_id,
                "fields": {
                    fn.get("liked_count", "点赞数"): str(liked),
                    fn.get("collected_count", "收藏数"): str(collected),
                    fn.get("comment_count", "评论数"): str(comment),
                    fn.get("interaction", "互动量"): str(interaction),
                    fn.get("hot", "热门"): is_hot,
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
        summary_only = self._feishu_cfg.get("summary_only", False)
        utils.logger.info(
            f"[Feishu] 开始写入: {safe_name} ({len(notes)} 条笔记"
            f", summary_only={summary_only})"
        )
        existing_table_id = None
        seq_offset = 0
        inserted = 0

        if not summary_only:
            existing_table_id = self._table_name_to_id.get(safe_name) if self._is_reusing else None

        if existing_table_id:
            utils.logger.info(f"[Feishu] {safe_name}: 查询已有记录...")
            note_to_record = self._get_existing_note_map(existing_table_id)
            existing_note_ids = set(note_to_record.keys())
            utils.logger.info(f"[Feishu] {safe_name}: 已有 {len(existing_note_ids)} 条记录")

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
            record = map_note_to_feishu_record(creator_name, note, field_names=self._fn_note)
            records.append(record)
            self._all_field_names.update(record.get("fields", {}).keys())

        if self.is_image_mode:
            skip_ids: set = set()
            if summary_only and self._is_reusing:
                skip_ids = (
                    set(self._video_summary_note_map.keys())
                    | set(self._image_text_summary_note_map.keys())
                )

            if skip_ids:
                new_pairs = [
                    (n, r) for n, r in zip(notes, records)
                    if n.get("note_id", "") not in skip_ids
                ]
                skip_count = len(notes) - len(new_pairs)
                if new_pairs:
                    up_notes, up_recs = zip(*new_pairs)
                    utils.logger.info(
                        f"[Feishu] {safe_name}: 仅上传 {len(up_notes)}/{len(notes)} "
                        f"条新笔记的附件 (跳过 {skip_count} 条已有)"
                    )
                    self._upload_images_for_records(list(up_notes), list(up_recs))
                    self._upload_videos_for_records(list(up_notes), list(up_recs))
                else:
                    utils.logger.info(
                        f"[Feishu] {safe_name}: 全部为已有记录，跳过附件上传"
                    )
                self._strip_string_attachments(records)
            else:
                utils.logger.info(f"[Feishu] {safe_name}: 开始上传图片/视频附件...")
                self._upload_images_for_records(notes, records)
                self._upload_videos_for_records(notes, records)
                utils.logger.info(f"[Feishu] {safe_name}: 附件上传完成")

        self._rebuild_field_defs()
        note_seq = self._fn_note.get("seq", "序号")
        ordered_with_serial = set(self._ordered_fields) | {note_seq}

        if not summary_only:
            if existing_table_id:
                table_id = existing_table_id
                for fn in self._ordered_fields:
                    if fn == note_seq:
                        continue
                    try:
                        self.client.add_field(
                            self.app_token, table_id, fn,
                            self._resolve_field_type(fn),
                        )
                    except Exception:
                        pass
            elif self._first_creator:
                # 先建第一张业务表，再删除多维表格默认数据表，保证所有表结构一致
                table_id = self.client.create_table(
                    self.app_token, safe_name, self._all_table_fields
                )
                tables = self.client.list_tables(self.app_token)
                default_table_id = next(
                    (t["table_id"] for t in tables if t.get("table_id") != table_id),
                    None,
                )
                if default_table_id:
                    try:
                        self.client.delete_table(self.app_token, default_table_id)
                        utils.logger.info("[Feishu] 已删除多维表格默认数据表")
                    except Exception as e:
                        utils.logger.warning(f"[Feishu] 删除默认表失败（不影响写入）: {e}")
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
                rec["fields"][note_seq] = str(i)

            self._ensure_table_fields(table_id, records)
            utils.logger.info(f"[Feishu] {safe_name}: 批量插入 {len(records)} 条记录到作者表...")
            inserted = self.client.batch_insert_records(
                self.app_token, table_id, records
            )
            self._total_inserted += inserted
            utils.logger.info(f"[Feishu] {safe_name}: 插入完成 ({inserted} 条成功)")

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

        self._creator_count += 1

        ct_name = self._fn_note.get("content_type", "内容类型")
        va_name = self._fn_note.get("video_attachment", "视频附件")
        creator_fname = self._fn_note.get("creator_name", "账号名称")
        video_records = []
        image_text_records = []
        for rec in records:
            fields = rec.get("fields", {})
            has_video = (
                fields.get(ct_name) == "视频"
                or fields.get(va_name)
            )
            summary_rec = {"fields": dict(fields)}
            summary_rec["fields"][creator_fname] = creator_name
            if has_video:
                video_records.append(summary_rec)
            else:
                image_text_records.append(summary_rec)

        if video_records:
            self._append_video_summary(video_records)
        if image_text_records:
            self._append_image_text_summary(image_text_records)

        if summary_only:
            parts = [f"[Pipeline] {safe_name}: {len(records)} 条仅写汇总表"]
        else:
            parts = [f"[Pipeline] {safe_name}: {inserted} 条写入完成"]
            if existing_table_id:
                parts.append("(追加到已有表)")
        if video_records:
            parts.append(f", {len(video_records)} 条视频入汇总表")
        if image_text_records:
            parts.append(f", {len(image_text_records)} 条图文入汇总表")
        utils.logger.info("".join(parts))

    def write_creator_async(self, creator_name: str, notes: List[Dict]):
        """异步版本：提交到后台队列，不阻塞主爬虫线程"""
        if not notes:
            return
        self._ensure_bitable()
        if self._write_executor is None:
            workers = int(self._feishu_cfg.get("write_workers", 2))
            self._write_executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="feishu-writer"
            )
            utils.logger.info(f"[Feishu] 写入线程池: {workers} 个并发线程")
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
                utils.logger.info(f"[Pipeline] 飞书写入进度: {i}/{total} 已完成")
            except Exception as e:
                import traceback
                utils.logger.error(
                    f"[Pipeline] 写入任务 {i} 异常: {e}\n"
                    f"{traceback.format_exc()}"
                )
        self._write_futures.clear()
        if self._write_executor:
            self._write_executor.shutdown(wait=False)
            self._write_executor = None

    # ==================== 搜索模式写入（笔记 + 评论两张表） ====================

    _SEARCH_NOTE_TYPES = [
        FIELD_TYPE_TEXT,  # seq 序号
        FIELD_TYPE_TEXT, FIELD_TYPE_TEXT, FIELD_TYPE_TEXT, FIELD_TYPE_SELECT,
        FIELD_TYPE_TEXT, FIELD_TYPE_TEXT, FIELD_TYPE_DATE, FIELD_TYPE_NUMBER,
        FIELD_TYPE_NUMBER, FIELD_TYPE_NUMBER, FIELD_TYPE_NUMBER, FIELD_TYPE_TEXT,
        FIELD_TYPE_TEXT, FIELD_TYPE_URL, FIELD_TYPE_TEXT,
    ]
    _COMMENT_TYPES = [
        FIELD_TYPE_TEXT,  # 序号
        FIELD_TYPE_TEXT, FIELD_TYPE_TEXT, FIELD_TYPE_SELECT, FIELD_TYPE_TEXT,
        FIELD_TYPE_TEXT, FIELD_TYPE_TEXT, FIELD_TYPE_DATE, FIELD_TYPE_TEXT,
        FIELD_TYPE_NUMBER, FIELD_TYPE_NUMBER, FIELD_TYPE_TEXT, FIELD_TYPE_TEXT,
    ]

    _SEARCH_NOTE_KEY_ORDER = [
        "seq", "keyword", "title", "desc", "content_type", "nickname", "user_id", "time",
        "liked_count", "collected_count", "comment_count", "share_count", "tag_list",
        "ip_location", "link", "comment_summary",
    ]
    _COMMENT_KEY_ORDER = [
        "seq",  # 序号，建表时必须有该列，插入时才会存在
        "note_id", "note_title", "level", "content", "nickname", "user_id", "time",
        "ip_location", "like_count", "sub_comment_count", "parent_comment_id", "pictures",
    ]

    def _get_search_note_fields(self):
        """从 config.feishu.field_names.search_note 构建笔记表字段列表"""
        return [
            {"field_name": self._fn_search_note.get(k, DEFAULT_FIELD_NAMES_SEARCH_NOTE.get(k, k)), "type": self._SEARCH_NOTE_TYPES[i]}
            for i, k in enumerate(self._SEARCH_NOTE_KEY_ORDER) if i < len(self._SEARCH_NOTE_TYPES)
        ]

    def _get_comment_fields(self):
        """从 config.feishu.field_names.comment 构建评论表字段列表"""
        return [
            {"field_name": self._fn_comment.get(k, DEFAULT_FIELD_NAMES_COMMENT.get(k, k)), "type": self._COMMENT_TYPES[i]}
            for i, k in enumerate(self._COMMENT_KEY_ORDER) if i < len(self._COMMENT_TYPES)
        ]

    def write_search_results(
        self,
        notes: List[Dict],
        comments_by_note: Dict[str, List[Dict]],
        note_table_name: str = "笔记数据",
        comment_table_name: str = "评论数据",
        overview_table_name: str = "数据概览",
    ):
        """
        搜索模式写入：笔记表 + 评论表 + 数据概览表

        Args:
            notes: 笔记数据列表
            comments_by_note: {note_id: [comment_dict, ...]} 评论按笔记分组
            note_table_name: 笔记数据表名称
            comment_table_name: 评论数据表名称
            overview_table_name: 数据概览表名称
        """
        if not notes:
            utils.logger.info("[Pipeline] 无笔记数据，跳过写入")
            return

        self._ensure_bitable()

        # --- 构建笔记记录（含评论摘要）---
        search_note_fields = self._get_search_note_fields()
        note_seq_name = self._fn_search_note.get("seq", "序号")

        # 附加图片字段（上传模式下）
        max_images = 0
        if self.is_image_mode:
            from tools.batch_crawler import _parse_image_urls
            for note in notes:
                cnt = len(_parse_image_urls(note.get("image_list", "")))
                if cnt > max_images:
                    max_images = cnt
        search_image_fields = []
        if self.is_image_mode and max_images > 0:
            search_image_fields = [
                {"field_name": f"图片{i}", "type": FIELD_TYPE_ATTACHMENT}
                for i in range(1, max_images + 1)
            ]

        note_records: List[Dict] = []
        for note in notes:
            note_id = note.get("note_id", "")
            note_comments = comments_by_note.get(note_id, [])
            summary = build_comment_summary(note_comments, top_n=5)
            record = map_search_note_to_feishu_record(note, summary, field_names=self._fn_search_note)
            note_records.append(record)

        # 上传图片/视频到飞书附件
        if self.is_image_mode and notes:
            self._upload_search_images(notes, note_records)
            self._upload_search_videos(notes, note_records, search_note_fields)

        # --- 构建评论记录（按 note_id 注入笔记标题，便于区分被评论的作品）---
        note_id_to_title = {
            n.get("note_id", ""): (n.get("title") or n.get("desc") or "")[:200]
            for n in notes
        }
        comment_fields = self._get_comment_fields()
        comment_seq_name = self._fn_comment.get("seq", "序号")
        comment_records: List[Dict] = []
        for note_id, clist in comments_by_note.items():
            note_title = note_id_to_title.get(note_id, "")
            ordered = sort_comments_first_level_then_replies(clist)
            for c in ordered:
                comment_with_title = {**c, "note_title": c.get("note_title") or note_title}
                comment_records.append(
                    map_comment_to_feishu_record(comment_with_title, field_names=self._fn_comment)
                )

        # --- 创建/复用笔记表（关键词搜索专用：只用 search_note 字段，不复用创作者模式表）---
        all_note_fields = search_note_fields + search_image_fields
        note_table_id = self._table_name_to_id.get(note_table_name)
        if note_table_id:
            utils.logger.info(
                f"[Pipeline] 复用已有笔记表: {note_table_name}"
            )
        else:
            # 搜索模式始终新建「笔记数据」表，使用 search_note 字段（序号、搜索关键词、作者昵称等），
            # 不复用 tables[0]，避免与创作者模式的「账号名称」等字段混用
            note_table_id = self.client.create_table(
                self.app_token, note_table_name,
                all_note_fields,
            )
            note_keep = {note_seq_name} | {fd["field_name"] for fd in all_note_fields}
            self.client.cleanup_default_fields_and_records(
                self.app_token, note_table_id, note_keep,
            )
            self._table_name_to_id[note_table_name] = note_table_id
            utils.logger.info(f"[Pipeline] 已创建搜索模式笔记表: {note_table_name}（字段: 序号、搜索关键词、标题、作者昵称等）")

        for i, rec in enumerate(note_records, 1):
            rec["fields"][note_seq_name] = str(i)

        note_inserted = self.client.batch_insert_records(
            self.app_token, note_table_id, note_records,
        )
        self._total_inserted += note_inserted
        utils.logger.info(
            f"[Pipeline] 笔记表写入: {note_inserted}/{len(note_records)} 条"
        )

        # --- 创建/复用评论表 ---
        if not comment_records:
            utils.logger.info("[Pipeline] 无评论数据，跳过评论表")
        else:
            comment_table_id = self._table_name_to_id.get(comment_table_name)
            if comment_table_id:
                utils.logger.info(
                    f"[Pipeline] 复用已有评论表: {comment_table_name}"
                )
            else:
                comment_table_id = self.client.create_table(
                    self.app_token, comment_table_name,
                    comment_fields,
                )
                comment_keep = {comment_seq_name} | {fd["field_name"] for fd in comment_fields}
                self.client.cleanup_default_fields_and_records(
                    self.app_token, comment_table_id, comment_keep,
                )
                self._table_name_to_id[comment_table_name] = comment_table_id

            for i, rec in enumerate(comment_records, 1):
                rec["fields"][comment_seq_name] = str(i)

            comment_inserted = self.client.batch_insert_records(
                self.app_token, comment_table_id, comment_records,
            )
            utils.logger.info(
                f"[Pipeline] 评论表写入: {comment_inserted}/{len(comment_records)} 条"
            )

        # --- 数据概览表（按互动量排名） ---
        self._write_search_overview(
            notes, comments_by_note, overview_table_name,
        )

        self._save_bitable_to_config()

    # ==================== 搜索模式: 图片/视频上传 ====================

    def _upload_search_images(
        self, notes: List[Dict], records: List[Dict]
    ):
        """搜索模式: 上传笔记图片到飞书附件字段"""
        from tools.batch_crawler import _parse_image_urls, _upload_note_images_to_feishu

        total_uploaded = 0
        total_failed = 0
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
                total_uploaded += 1
            expected = len(image_urls)
            uploaded = len(image_tokens)
            if uploaded < expected:
                total_failed += expected - uploaded

        # 清理未成功上传的纯文本 URL 占位（飞书附件字段不接受字符串）
        for record in records:
            for key in list(record.get("fields", {}).keys()):
                if key.startswith("图片") and isinstance(record["fields"][key], str):
                    del record["fields"][key]

        if total_uploaded > 0 or total_failed > 0:
            utils.logger.info(
                f"[Pipeline] 搜索图片上传: {total_uploaded} 张成功, {total_failed} 张失败"
            )

    def _upload_search_videos(
        self, notes: List[Dict], records: List[Dict],
        search_note_fields: List[Dict],
    ):
        """搜索模式: 上传笔记视频到飞书附件字段"""
        from tools.batch_crawler import _upload_note_video_to_feishu

        has_video = any(
            n.get("type") == "video" or n.get("video_url")
            for n in notes
        )
        if not has_video:
            return

        video_field_name = "视频附件"
        # 确保字段定义中包含视频附件
        if not any(fd["field_name"] == video_field_name for fd in search_note_fields):
            search_note_fields.append(
                {"field_name": video_field_name, "type": FIELD_TYPE_ATTACHMENT}
            )

        total_uploaded = 0
        total_skipped = 0
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
                record["fields"][video_field_name] = attachment
                total_uploaded += 1
            else:
                total_skipped += 1
            time.sleep(0.15)  # 轻微节流，避免飞书 API 限流

        # 清理未成功上传的纯文本占位
        for record in records:
            flds = record.get("fields", {})
            if video_field_name in flds and isinstance(flds[video_field_name], str):
                del flds[video_field_name]

        if total_uploaded > 0 or total_skipped > 0:
            utils.logger.info(
                f"[Pipeline] 搜索视频上传: {total_uploaded} 个成功, {total_skipped} 个跳过"
            )

    # ==================== 搜索模式: 数据概览表 ====================

    def _write_search_overview(
        self,
        notes: List[Dict],
        comments_by_note: Dict[str, List[Dict]],
        table_name: str,
    ):
        """按互动量排名生成数据概览表，含统计摘要行"""

        def _safe_int(v):
            if v is None or v == "":
                return 0
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        # 计算互动量并排序
        enriched = []
        for n in notes:
            liked = _safe_int(n.get("liked_count", 0))
            collected = _safe_int(n.get("collected_count", 0))
            comment_cnt = _safe_int(n.get("comment_count", 0))
            share_cnt = _safe_int(n.get("share_count", 0))
            interaction = liked + collected + comment_cnt + share_cnt
            enriched.append({**n, "_interaction": interaction})
        enriched.sort(key=lambda x: x["_interaction"], reverse=True)

        # 统计摘要
        total_notes = len(notes)
        total_comments = sum(len(v) for v in comments_by_note.values())
        all_interactions = [e["_interaction"] for e in enriched]
        avg_interaction = sum(all_interactions) / total_notes if total_notes else 0
        max_liked = max((_safe_int(n.get("liked_count", 0)) for n in notes), default=0)
        max_collected = max((_safe_int(n.get("collected_count", 0)) for n in notes), default=0)

        # 概览表字段
        overview_fields = [
            {"field_name": "排名", "type": FIELD_TYPE_NUMBER},
            {"field_name": "标题", "type": FIELD_TYPE_TEXT},
            {"field_name": "作者昵称", "type": FIELD_TYPE_TEXT},
            {"field_name": "内容类型", "type": FIELD_TYPE_SELECT},
            {"field_name": "点赞数", "type": FIELD_TYPE_NUMBER},
            {"field_name": "收藏数", "type": FIELD_TYPE_NUMBER},
            {"field_name": "评论数", "type": FIELD_TYPE_NUMBER},
            {"field_name": "分享数", "type": FIELD_TYPE_NUMBER},
            {"field_name": "互动总量", "type": FIELD_TYPE_NUMBER},
            {"field_name": "链接", "type": FIELD_TYPE_URL},
            {"field_name": "搜索关键词", "type": FIELD_TYPE_TEXT},
        ]

        overview_table_id = self.client.create_table(
            self.app_token, table_name, overview_fields,
        )
        overview_keep = {fd["field_name"] for fd in overview_fields}
        # 清理默认空记录和多余字段，但不依赖主字段改名
        try:
            records = self.client.list_records(self.app_token, overview_table_id)
            if records:
                self.client.batch_delete_records(
                    self.app_token, overview_table_id,
                    [r["record_id"] for r in records],
                )
        except Exception:
            pass
        self._table_name_to_id[table_name] = overview_table_id

        # 摘要行（排名=0 表示这是统计行）
        summary_record = {"fields": {
            "排名": 0,
            "标题": f"共 {total_notes} 篇笔记, {total_comments} 条评论",
            "作者昵称": f"平均互动: {avg_interaction:.0f}",
            "点赞数": max_liked,
            "收藏数": max_collected,
            "评论数": total_comments,
            "互动总量": sum(all_interactions),
            "搜索关键词": enriched[0].get("source_keyword", "") if enriched else "",
        }}

        # 排名行
        ranking_records = [summary_record]
        for rank, n in enumerate(enriched, 1):
            note_url = n.get("note_url", "")
            note_type = n.get("type", "")
            ranking_records.append({"fields": {
                "排名": rank,
                "标题": str(n.get("title", "") or n.get("desc", ""))[:200],
                "作者昵称": str(n.get("nickname", "")),
                "内容类型": "视频" if note_type == "video" else "图片",
                "点赞数": _safe_int(n.get("liked_count", 0)),
                "收藏数": _safe_int(n.get("collected_count", 0)),
                "评论数": _safe_int(n.get("comment_count", 0)),
                "分享数": _safe_int(n.get("share_count", 0)),
                "互动总量": n["_interaction"],
                "链接": {"link": note_url, "text": note_url} if note_url else "",
                "搜索关键词": str(n.get("source_keyword", "")),
            }})

        inserted = self.client.batch_insert_records(
            self.app_token, overview_table_id, ranking_records,
        )
        utils.logger.info(
            f"[Pipeline] 数据概览表写入: {inserted} 条 (含统计摘要 + {len(enriched)} 条排名)"
        )

    # ==================== 图片/视频上传 ====================

    @staticmethod
    def _strip_string_attachments(records: List[Dict]):
        """清理记录中未上传的字符串类型附件字段（图片URL、视频URL）"""
        for record in records:
            flds = record.get("fields", {})
            for key in list(flds.keys()):
                if key.startswith("图片") and isinstance(flds[key], str):
                    del flds[key]
                if key == "视频附件" and isinstance(flds[key], str):
                    del flds[key]

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
            time.sleep(0.15)  # 轻微节流，避免飞书 API 限流

        for record in records:
            flds = record.get("fields", {})
            if "视频附件" in flds and isinstance(flds["视频附件"], str):
                del flds["视频附件"]

    # ==================== 图文汇总表 ====================

    def _append_image_text_summary(self, image_text_records: List[Dict]):
        """将图文类笔记追加到「图文汇总」表"""
        if not image_text_records:
            return
        utils.logger.info(f"[Feishu] 图文汇总: 处理 {len(image_text_records)} 条记录...")

        with self._summary_lock:
            if not self.image_text_summary_table_id:
                self.image_text_summary_table_id = self._create_summary_table("图文汇总")
                if not self.image_text_summary_table_id:
                    return

            new_records = self._dedup_and_update_summary(
                image_text_records, self._image_text_summary_note_map,
                self.image_text_summary_table_id,
            )
            skipped = len(image_text_records) - len(new_records)

            if not new_records:
                if skipped:
                    utils.logger.info(f"[Pipeline] 图文汇总: {skipped} 条已存在(互动量已更新)")
                return

            note_seq = self._fn_note.get("seq", "序号")
            for rec in new_records:
                self._image_text_serial += 1
                rec["fields"][note_seq] = str(self._image_text_serial)

            self._ensure_table_fields(self.image_text_summary_table_id, new_records)
            inserted = self.client.batch_insert_records(
                self.app_token, self.image_text_summary_table_id,
                new_records
            )
        parts = [f"[Pipeline] 图文汇总: {inserted} 条写入"]
        if skipped:
            parts.append(f", {skipped} 条已存在(互动量已更新)")
        utils.logger.info("".join(parts))

    # ==================== 视频汇总 + 脚本流水线 ====================

    def _append_video_summary(self, video_records: List[Dict]):
        utils.logger.info(f"[Feishu] 视频汇总: 处理 {len(video_records)} 条记录...")
        with self._summary_lock:
            if not self.summary_table_id:
                self.summary_table_id = self._create_summary_table("视频汇总")
                if not self.summary_table_id:
                    return
                try:
                    self.client.add_field(
                        self.app_token, self.summary_table_id, "视频公网链接", 15
                    )
                except Exception:
                    pass

            new_records = self._dedup_and_update_summary(
                video_records, self._video_summary_note_map,
                self.summary_table_id,
            )
            video_skipped = len(video_records) - len(new_records)

            if not new_records:
                if video_skipped:
                    utils.logger.info(f"[Pipeline] 视频汇总: {video_skipped} 条已存在(互动量已更新)")
                return

            note_seq = self._fn_note.get("seq", "序号")
            for vr in new_records:
                self._video_serial += 1
                vr["fields"][note_seq] = str(self._video_serial)

            self._ensure_table_fields(self.summary_table_id, new_records)
            inserted = self.client.batch_insert_records_full(
                self.app_token, self.summary_table_id, new_records
            )

        hot_count = 0
        skip_count = 0
        for rec in inserted:
            record_id = rec.get("record_id", "")
            fields = rec.get("fields", {})
            video_attach = fields.get(self._fn_note.get("video_attachment", "视频附件"))
            if not video_attach or not isinstance(video_attach, list):
                continue
            ft = video_attach[0].get("file_token", "")
            if not ft or not record_id:
                continue

            # 根据配置决定是否只对热门视频提取脚本
            if self._script_hot_only:
                hot_raw = fields.get(self._fn_note.get("hot", "热门"), "")
                if isinstance(hot_raw, list):
                    hot_val = "".join(
                        seg.get("text", "") if isinstance(seg, dict)
                        else str(seg) for seg in hot_raw
                    ).strip()
                else:
                    hot_val = str(hot_raw).strip() if hot_raw else ""
                if not hot_val:
                    skip_count += 1
                    continue

            title_raw = fields.get(self._fn_note.get("title", "标题"), "")
            if isinstance(title_raw, list):
                title = "".join(
                    seg.get("text", "") if isinstance(seg, dict)
                    else str(seg)
                    for seg in title_raw
                )
            else:
                title = str(title_raw) if title_raw else ""

            account_raw = fields.get(self._fn_note.get("creator_name", "账号名称"), "")
            account = str(account_raw) if account_raw else ""

            self._pending_videos.append({
                "record_id": record_id,
                "file_token": ft,
                "title": title,
                "account": account,
            })
            hot_count += 1

        if skip_count > 0:
            utils.logger.info(
                f"[Pipeline] 视频脚本: {hot_count} 条热门待提取, "
                f"{skip_count} 条非热门跳过"
            )

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

        for tid, label in [
            (self.summary_table_id, "热门视频"),
            (self.image_text_summary_table_id, "热门图文"),
        ]:
            if not tid:
                continue
            try:
                sf = self.client.list_fields(self.app_token, tid)
                hot_fid = ""
                for f in sf:
                    if f and f.get("field_name") == "热门":
                        hot_fid = f.get("field_id", "")
                        break
                if hot_fid:
                    self.client.create_view(
                        self.app_token, tid,
                        view_name=f"🔥 {label}",
                        filter_conditions=[{
                            "field_id": hot_fid,
                            "operator": "isNotEmpty",
                        }],
                    )
            except Exception:
                pass

        if self.summary_table_id:
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

        if self._transfer_owner and self._owner_open_id and self.app_token:
            try:
                self.client.transfer_owner(
                    self.app_token, self._owner_open_id,
                )
            except Exception as e:
                utils.logger.warning(
                    f"[Pipeline] 转移所有者失败: {e}"
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
                    if name not in ("视频汇总", "图文汇总")
                },
                "summary_table_id": self.summary_table_id or "",
                "image_text_summary_table_id": self.image_text_summary_table_id or "",
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
        # 不再重复全量扫描，batch 后处理 _run_video_script_extraction 会兜底
        utils.logger.info(
            "[Pipeline] 跳过最终全量扫描（由 batch 后处理统一兜底）"
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
        # 默认转移所有者给 owner_open_id；仅当 transfer_owner=false 时不转移
        if self._transfer_owner and self.app_token and self._owner_open_id:
            try:
                self.client.transfer_owner(
                    self.app_token, self._owner_open_id,
                )
            except Exception as e:
                utils.logger.warning(f"[Pipeline] 转移所有者失败: {e}")
        self.close()
