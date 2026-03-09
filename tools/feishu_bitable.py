# -*- coding: utf-8 -*-
"""
飞书多维表格 API 客户端
支持：创建多维表格、添加字段、批量写入记录
文档：https://open.feishu.cn/document/server-docs/docs/bitable-v1/bitable-overview
"""
import json
import time
from typing import List, Dict, Any, Optional

import httpx

from tools import utils


class FeishuBitableClient:
    """飞书多维表格 API 客户端"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        """
        Args:
            app_id: 飞书应用 App ID
            app_secret: 飞书应用 App Secret
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token: Optional[str] = None
        self._token_expire_time: float = 0
        self._client = httpx.Client(timeout=30.0)

    # ==================== 认证 ====================

    def _get_tenant_access_token(self) -> str:
        """获取 tenant_access_token（自动缓存，过期刷新）"""
        if self._tenant_access_token and time.time() < self._token_expire_time:
            return self._tenant_access_token

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        resp = self._client.post(url, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        })
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"获取 tenant_access_token 失败: {data.get('msg')}")

        self._tenant_access_token = data["tenant_access_token"]
        # 提前 5 分钟刷新
        self._token_expire_time = time.time() + data.get("expire", 7200) - 300
        utils.logger.info("[FeishuBitable] 获取 tenant_access_token 成功")
        return self._tenant_access_token

    def _headers(self) -> Dict[str, str]:
        """带认证的请求头"""
        return {
            "Authorization": f"Bearer {self._get_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _request(self, method: str, url: str, **kwargs) -> Dict:
        """统一请求方法，带错误处理和重试"""
        headers = self._headers()
        resp = self._client.request(method, url, headers=headers, **kwargs)
        data = resp.json()

        if data.get("code") != 0:
            utils.logger.error(f"[FeishuBitable] API 错误: {data}")
            raise Exception(f"飞书 API 错误 (code={data.get('code')}): {data.get('msg')}")

        return data.get("data", {})

    # ==================== 多维表格操作 ====================

    def create_bitable(self, name: str, folder_token: Optional[str] = None) -> Dict[str, str]:
        """
        创建多维表格
        
        Args:
            name: 表格名称
            folder_token: 文件夹 token（可选，为空则创建在根目录）
            
        Returns:
            {"app_token": "xxx", "url": "xxx"}
        """
        url = f"{self.BASE_URL}/bitable/v1/apps"
        body: Dict[str, Any] = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token

        data = self._request("POST", url, json=body)
        app_token = data.get("app", {}).get("app_token", "")
        app_url = data.get("app", {}).get("url", "")

        utils.logger.info(f"[FeishuBitable] 创建多维表格成功: {name} (token={app_token})")
        return {"app_token": app_token, "url": app_url}

    def list_tables(self, app_token: str) -> List[Dict[str, str]]:
        """
        列出多维表格中的所有数据表
        
        Returns:
            [{"table_id": "xxx", "name": "xxx"}, ...]
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables"
        data = self._request("GET", url)
        items = data.get("items", [])
        return [{"table_id": t.get("table_id", ""), "name": t.get("name", "")} for t in items]

    def create_table(self, app_token: str, table_name: str,
                     fields: List[Dict[str, str]]) -> str:
        """
        在多维表格中创建数据表，并定义字段
        
        Args:
            app_token: 多维表格 token
            table_name: 数据表名称
            fields: 字段定义列表 [{"field_name": "xxx", "type": 1}]
            
        Returns:
            table_id
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables"
        body = {
            "table": {
                "name": table_name,
                "fields": fields,
            }
        }

        data = self._request("POST", url, json=body)
        table_id = data.get("table_id", "")
        utils.logger.info(f"[FeishuBitable] 创建数据表成功: {table_name} (id={table_id})")
        return table_id

    def add_field(self, app_token: str, table_id: str,
                  field_name: str, field_type: int = 1,
                  description: str = "") -> str:
        """
        添加字段
        
        Args:
            app_token: 多维表格 token
            table_id: 数据表 ID
            field_name: 字段名
            field_type: 字段类型 (1=文本, 15=链接)
            description: 字段描述
            
        Returns:
            field_id
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        body: Dict[str, Any] = {
            "field_name": field_name,
            "type": field_type,
        }
        if description:
            body["description"] = {"text": description}

        data = self._request("POST", url, json=body)
        field_id = data.get("field", {}).get("field_id", "")
        utils.logger.info(f"[FeishuBitable] 添加字段: {field_name} (id={field_id})")
        return field_id

    def batch_insert_records(self, app_token: str, table_id: str,
                             records: List[Dict[str, Any]],
                             batch_size: int = 100) -> int:
        """
        批量插入记录
        
        Args:
            app_token: 多维表格 token
            table_id: 数据表 ID
            records: 记录列表 [{"fields": {"字段名": "值"}}, ...]
            batch_size: 每批大小（飞书限制最多 500）
            
        Returns:
            成功插入的记录数
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        total_inserted = 0

        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            body = {"records": batch}

            try:
                data = self._request("POST", url, json=body)
                inserted = len(data.get("records", []))
                total_inserted += inserted
                utils.logger.info(
                    f"[FeishuBitable] 批量写入第 {i // batch_size + 1} 批: "
                    f"{inserted} 条 (总计 {total_inserted}/{len(records)})"
                )
            except Exception as e:
                utils.logger.error(f"[FeishuBitable] 批量写入失败 (批次 {i // batch_size + 1}): {e}")

            # 简单限流
            if i + batch_size < len(records):
                time.sleep(0.5)

        return total_inserted

    def batch_insert_records_full(self, app_token: str, table_id: str,
                                  records: List[Dict[str, Any]],
                                  batch_size: int = 100) -> List[Dict]:
        """
        批量插入记录，返回包含 record_id 的完整记录列表

        Returns:
            写入成功的记录列表 [{"record_id": "xxx", "fields": {...}}, ...]
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        all_inserted: List[Dict] = []

        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            body = {"records": batch}
            try:
                data = self._request("POST", url, json=body)
                inserted = data.get("records", [])
                all_inserted.extend(inserted)
                utils.logger.info(
                    f"[FeishuBitable] 批量写入第 {i // batch_size + 1} 批: "
                    f"{len(inserted)} 条 (总计 {len(all_inserted)}/{len(records)})"
                )
            except Exception as e:
                utils.logger.error(
                    f"[FeishuBitable] 批量写入失败 (批次 {i // batch_size + 1}): {e}"
                )
            if i + batch_size < len(records):
                time.sleep(0.5)

        return all_inserted

    def list_fields(self, app_token: str, table_id: str) -> List[Dict]:
        """列出数据表的所有字段"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        data = self._request("GET", url)
        return data.get("items", [])

    def delete_field(self, app_token: str, table_id: str, field_id: str):
        """删除字段"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}"
        self._request("DELETE", url)

    def list_records(self, app_token: str, table_id: str, page_size: int = 100) -> List[Dict]:
        """列出记录"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        data = self._request("GET", url, params={"page_size": page_size})
        return data.get("items", [])

    def batch_delete_records(self, app_token: str, table_id: str, record_ids: List[str]):
        """批量删除记录"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
        self._request("POST", url, json={"records": record_ids})

    def update_field(self, app_token: str, table_id: str, field_id: str,
                     field_name: str, field_type: int = 1):
        """更新字段名称/类型"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}"
        body = {"field_name": field_name, "type": field_type}
        self._request("PUT", url, json=body)

    def cleanup_default_fields_and_records(self, app_token: str, table_id: str, keep_field_names: set):
        """清理默认字段和空记录，主字段改为序号"""
        # 删除默认空记录
        try:
            records = self.list_records(app_token, table_id)
            if records:
                record_ids = [r["record_id"] for r in records]
                self.batch_delete_records(app_token, table_id, record_ids)
                utils.logger.info(f"[FeishuBitable] 删除 {len(record_ids)} 条默认空记录")
        except Exception as e:
            utils.logger.warning(f"[FeishuBitable] 删除默认记录失败: {e}")

        # 处理默认字段
        try:
            fields = self.list_fields(app_token, table_id)
            for field in fields:
                if not field or not isinstance(field, dict):
                    continue
                fname = field.get("field_name", "")
                fid = field.get("field_id", "")
                if not fid:
                    continue
                if fname in keep_field_names:
                    continue
                # 主字段不能删除，改名为"序号"
                is_primary = field.get("is_primary", False) or (field.get("property") or {}).get("is_primary", False)
                if is_primary:
                    try:
                        self.update_field(app_token, table_id, fid, "序号", 1)
                        utils.logger.info(f"[FeishuBitable] 主字段改名: {fname} → 序号")
                    except Exception:
                        pass
                else:
                    try:
                        self.delete_field(app_token, table_id, fid)
                        utils.logger.info(f"[FeishuBitable] 删除默认字段: {fname}")
                    except Exception:
                        pass
        except Exception as e:
            utils.logger.warning(f"[FeishuBitable] 清理默认字段失败: {e}")

    def create_view(self, app_token: str, table_id: str,
                    view_name: str, view_type: str = "grid",
                    filter_conditions: List[Dict] = None,
                    filter_conjunction: str = "and") -> str:
        """
        创建视图并设置筛选条件
        
        Args:
            app_token: 多维表格 token
            table_id: 数据表 ID
            view_name: 视图名称
            view_type: 视图类型 (grid=表格, kanban=看板, gallery=画册)
            filter_conditions: 筛选条件列表
            filter_conjunction: 条件关系 (and/or)
            
        Returns:
            view_id
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/views"
        body = {"view_name": view_name, "view_type": view_type}
        data = self._request("POST", url, json=body)
        view_id = data.get("view", {}).get("view_id", "")
        utils.logger.info(f"[FeishuBitable] 创建视图: {view_name} (id={view_id})")

        # 设置筛选条件
        if filter_conditions and view_id:
            filter_url = f"{url}/{view_id}"
            filter_body = {
                "view_name": view_name,
                "property": {
                    "filter_info": {
                        "conjunction": filter_conjunction,
                        "conditions": filter_conditions,
                    }
                }
            }
            try:
                self._request("PATCH", filter_url, json=filter_body)
                utils.logger.info(f"[FeishuBitable] 视图筛选条件已设置")
            except Exception as e:
                utils.logger.warning(f"[FeishuBitable] 设置视图筛选失败: {e}")

        return view_id

    def list_tables(self, app_token: str) -> List[Dict]:
        """列出多维表格中的所有数据表"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables"
        data = self._request("GET", url)
        return data.get("items", [])

    def rename_table(self, app_token: str, table_id: str, new_name: str):
        """重命名数据表"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}"
        self._request("PATCH", url, json={"name": new_name})

    # ==================== 媒体上传 ====================

    def upload_media(self, app_token: str, file_path: str,
                     file_name: str = "", parent_type: str = "bitable_file",
                     ) -> str:
        """
        上传文件到飞书，获取 file_token
        
        Args:
            app_token: 多维表格 token（作为 parent_node）
            file_path: 本地文件路径
            file_name: 文件名（为空则从路径提取）
            parent_type: 父节点类型
            
        Returns:
            file_token
        """
        import os
        if not file_name:
            file_name = os.path.basename(file_path)

        file_size = os.path.getsize(file_path)
        url = f"{self.BASE_URL}/drive/v1/medias/upload_all"
        headers = {"Authorization": f"Bearer {self._get_tenant_access_token()}"}

        with open(file_path, "rb") as f:
            files = {
                "file_name": (None, file_name),
                "parent_type": (None, parent_type),
                "parent_node": (None, app_token),
                "size": (None, str(file_size)),
                "file": (file_name, f, "application/octet-stream"),
            }
            resp = self._client.post(url, headers=headers, files=files)

        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"上传文件失败: {data.get('msg')}")

        file_token = data.get("data", {}).get("file_token", "")
        return file_token

    def upload_image_from_url(self, app_token: str, image_url: str,
                               temp_dir: str = "/tmp/feishu_images") -> str:
        """
        从URL下载图片并上传到飞书
        
        Args:
            app_token: 多维表格 token
            image_url: 图片URL
            temp_dir: 临时下载目录
            
        Returns:
            file_token（失败返回空字符串）
        """
        import os
        import hashlib
        os.makedirs(temp_dir, exist_ok=True)

        try:
            # 下载图片（小红书CDN需要Referer头防403）
            download_headers = {
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }
            resp = self._client.get(
                image_url, timeout=30.0, follow_redirects=True,
                headers=download_headers
            )
            if resp.status_code != 200:
                utils.logger.warning(
                    f"[FeishuBitable] 图片下载失败 HTTP {resp.status_code}: {image_url[:80]}"
                )
                return ""

            # 用URL hash作文件名
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
            content_type = resp.headers.get("content-type", "image/jpeg")
            ext = ".jpg"
            if "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            elif "gif" in content_type:
                ext = ".gif"

            file_path = os.path.join(temp_dir, f"{url_hash}{ext}")
            with open(file_path, "wb") as f:
                f.write(resp.content)

            # 上传到飞书
            file_token = self.upload_media(app_token, file_path)

            # 清理临时文件
            try:
                os.remove(file_path)
            except Exception:
                pass

            return file_token

        except Exception as e:
            utils.logger.warning(f"[FeishuBitable] 图片处理失败: {e}")
            return ""

    def update_record(self, app_token: str, table_id: str,
                      record_id: str, fields: Dict[str, Any]):
        """更新单条记录"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        body = {"fields": fields}
        self._request("PUT", url, json=body)

    def batch_update_records(self, app_token: str, table_id: str,
                             records: List[Dict[str, Any]],
                             batch_size: int = 100) -> int:
        """
        批量更新记录
        
        Args:
            records: [{"record_id": "xxx", "fields": {"字段名": "值"}}]
        Returns:
            成功更新的记录数
        """
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
        total_updated = 0
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            body = {"records": batch}
            try:
                data = self._request("POST", url, json=body)
                updated = len(data.get("records", []))
                total_updated += updated
            except Exception as e:
                utils.logger.error(f"[FeishuBitable] 批量更新失败 (批次 {i // batch_size + 1}): {e}")
            if i + batch_size < len(records):
                time.sleep(0.5)
        return total_updated

    def list_all_records(self, app_token: str, table_id: str,
                         page_size: int = 100) -> List[Dict]:
        """列出数据表的所有记录（自动分页）"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        all_records = []
        page_token = None
        while True:
            params: Dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", url, params=params)
            items = data.get("items", [])
            all_records.extend(items)
            if not data.get("has_more", False):
                break
            page_token = data.get("page_token")
        return all_records

    def batch_get_tmp_download_url(self, file_tokens: List[str]) -> Dict[str, str]:
        """
        批量获取文件的临时公网下载链接
        
        Args:
            file_tokens: file_token 列表
            
        Returns:
            {file_token: tmp_download_url} 映射
        """
        if not file_tokens:
            return {}

        result = {}
        # API 实际限制每次最多 5 个（文档标称 50，实测不超过 5）
        url = f"{self.BASE_URL}/drive/v1/medias/batch_get_tmp_download_url"
        batch_size = 5
        for i in range(0, len(file_tokens), batch_size):
            batch = file_tokens[i:i + batch_size]
            try:
                # httpx 用 tuple list 传递重复的 query key
                params = [("file_tokens", t) for t in batch]
                data = self._request("GET", url, params=params)
                for item in data.get("tmp_download_urls", []):
                    token = item.get("file_token", "")
                    tmp_url = item.get("tmp_download_url", "")
                    if token and tmp_url:
                        result[token] = tmp_url
            except Exception as e:
                utils.logger.warning(
                    f"[FeishuBitable] 获取临时下载链接失败 (批次 {i // batch_size + 1}): {e}"
                )
            if i + batch_size < len(file_tokens):
                time.sleep(0.3)

        utils.logger.info(
            f"[FeishuBitable] 获取临时下载链接: {len(result)}/{len(file_tokens)} 个成功"
        )
        return result

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ==================== 字段类型常量 ====================
FIELD_TYPE_TEXT = 1           # 多行文本
FIELD_TYPE_NUMBER = 2         # 数字
FIELD_TYPE_SELECT = 3         # 单选
FIELD_TYPE_MULTI_SELECT = 4   # 多选
FIELD_TYPE_DATE = 5           # 日期
FIELD_TYPE_CHECKBOX = 7       # 复选框
FIELD_TYPE_URL = 15           # 超链接
FIELD_TYPE_ATTACHMENT = 17    # 附件
FIELD_TYPE_AUTO_NUMBER = 1005 # 自动编号


def map_field_type(field_def: Dict[str, str]) -> int:
    """
    将 Excel 字段定义映射为飞书字段类型
    
    Args:
        field_def: {"name": "xxx", "type": "text/url/number/date", "description": ""}
    """
    type_str = field_def.get("type", "text").lower()
    mapping = {
        "text": FIELD_TYPE_TEXT,
        "url": FIELD_TYPE_URL,
        "number": FIELD_TYPE_NUMBER,
        "date": FIELD_TYPE_DATE,
        "select": FIELD_TYPE_SELECT,
    }
    return mapping.get(type_str, FIELD_TYPE_TEXT)


def build_feishu_fields(field_defs: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """
    将 Excel 读取的字段定义转换为飞书 API 格式
    
    Args:
        field_defs: [{"name": "xxx", "type": "text", "description": ""}]
        
    Returns:
        [{"field_name": "xxx", "type": 1}]
    """
    feishu_fields = []
    for fd in field_defs:
        field = {
            "field_name": fd["name"],
            "type": map_field_type(fd),
        }
        feishu_fields.append(field)
    return feishu_fields


def build_record(field_names: List[str], data: Dict[str, Any]) -> Dict[str, Any]:
    """
    构建单条飞书记录
    
    Args:
        field_names: 字段名列表
        data: 爬取的数据字典
        
    Returns:
        {"fields": {"字段名": "值"}}
    """
    fields = {}
    for name in field_names:
        value = data.get(name, "")
        if value is None:
            value = ""
        # 飞书链接类型需要特殊格式
        if isinstance(value, str) and value.startswith("http"):
            fields[name] = {"link": value, "text": value}
        else:
            fields[name] = str(value)
    return {"fields": fields}


# ==================== 数据映射 ====================

def map_note_to_feishu_record(creator_name: str, note_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    将爬取的笔记数据映射为飞书多维表格记录
    
    字段映射（根据 Excel Sheet2 的定义）:
    账号名称 | 内容类型 | 标题 | 正文 | 标签 | 链接 | 发布时间 | 图片 | 视频脚本
    """
    # 内容类型
    note_type = note_data.get("type", "")
    content_type = "视频" if note_type == "video" else "图片"

    # 发布时间：飞书日期字段需要毫秒时间戳
    time_raw = note_data.get("time", "")
    time_ms = None
    if isinstance(time_raw, (int, float)) and time_raw > 0:
        time_ms = int(time_raw) if time_raw > 1e12 else int(time_raw * 1000)

    # 图片拆分为独立字段
    image_raw = note_data.get("image_list", "")
    if isinstance(image_raw, list):
        image_urls = [url for url in image_raw if url and str(url).startswith("http")]
    elif image_raw:
        image_urls = [url.strip() for url in str(image_raw).split(",") if url.strip().startswith("http")]
    else:
        image_urls = []

    # 链接
    note_url = note_data.get("note_url", "")

    # 互动数据
    def _safe_int(v):
        if v is None or v == "": return 0
        try: return int(v)
        except: return 0

    liked = _safe_int(note_data.get("liked_count", 0))
    collected = _safe_int(note_data.get("collected_count", 0))
    comment = _safe_int(note_data.get("comment_count", 0))
    interaction = liked + collected + comment
    is_hot = "🔥 热门" if interaction >= 50 else ""

    fields: Dict[str, Any] = {
        "账号名称": creator_name,
        "内容类型": content_type,
        "标题": note_data.get("title", ""),
        "正文": note_data.get("desc", ""),
        "标签": note_data.get("tag_list", ""),
        "链接": {"link": note_url, "text": note_url} if note_url else "",
        "发布时间": time_ms if time_ms else "",
        "点赞数": str(liked),
        "收藏数": str(collected),
        "评论数": str(comment),
        "互动量": str(interaction),
        "热门": is_hot,
        "视频脚本": "",
    }

    # 附件字段（预留，后续存视频文件）
    # 注：附件类型字段不能写字符串，留空不写入
    
    # 动态图片字段
    for i, url in enumerate(image_urls, 1):
        fields[f"图片{i}"] = url

    # 序号字段（由外部在批量写入时填充）
    fields["序号"] = ""

    return {"fields": fields}


# ==================== 搜索模式笔记映射（B 部门） ====================

def map_search_note_to_feishu_record(
    note_data: Dict[str, Any],
    comment_summary: str = "",
) -> Dict[str, Any]:
    """
    将搜索模式爬取的笔记数据映射为飞书多维表格记录（含评论摘要）
    """
    note_type = note_data.get("type", "")
    content_type = "视频" if note_type == "video" else "图片"

    time_raw = note_data.get("time", "")
    time_ms = None
    if isinstance(time_raw, (int, float)) and time_raw > 0:
        time_ms = int(time_raw) if time_raw > 1e12 else int(time_raw * 1000)

    note_url = note_data.get("note_url", "")

    def _safe_int(v):
        if v is None or v == "":
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    liked = _safe_int(note_data.get("liked_count", 0))
    collected = _safe_int(note_data.get("collected_count", 0))
    comment_count = _safe_int(note_data.get("comment_count", 0))
    share_count = _safe_int(note_data.get("share_count", 0))

    fields: Dict[str, Any] = {
        "序号": "",
        "搜索关键词": str(note_data.get("source_keyword", "")),
        "标题": str(note_data.get("title", "")),
        "正文": str(note_data.get("desc", "")),
        "内容类型": content_type,
        "作者昵称": str(note_data.get("nickname", "")),
        "作者ID": str(note_data.get("user_id", "")),
        "发布时间": time_ms if time_ms else "",
        "点赞数": str(liked),
        "收藏数": str(collected),
        "评论数": str(comment_count),
        "分享数": str(share_count),
        "标签": str(note_data.get("tag_list", "")),
        "IP属地": str(note_data.get("ip_location", "")),
        "链接": {"link": note_url, "text": note_url} if note_url else "",
        "评论摘要": comment_summary,
    }

    return {"fields": fields}


# ==================== 评论数据映射 ====================

def map_comment_to_feishu_record(comment_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    将爬取的评论数据映射为飞书多维表格记录

    字段映射:
    笔记ID | 笔记标题 | 评论级别 | 评论内容 | 评论者昵称 | 评论者ID |
    评论时间 | IP属地 | 点赞数 | 二级评论数 | 父评论ID | 评论图片
    """
    parent_id = comment_data.get("parent_comment_id", 0)
    is_sub = parent_id and parent_id != 0 and str(parent_id) != "0"
    level = "二级评论" if is_sub else "一级评论"

    time_raw = comment_data.get("create_time", "")
    time_ms = None
    if isinstance(time_raw, (int, float)) and time_raw > 0:
        time_ms = int(time_raw) if time_raw > 1e12 else int(time_raw * 1000)

    def _safe_int(v):
        if v is None or v == "":
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    fields: Dict[str, Any] = {
        "序号": "",
        "笔记ID": str(comment_data.get("note_id", "")),
        "笔记标题": str(comment_data.get("note_title", "")),
        "评论级别": level,
        "评论内容": str(comment_data.get("content", "")),
        "评论者昵称": str(comment_data.get("nickname", "")),
        "评论者ID": str(comment_data.get("user_id", "")),
        "评论时间": time_ms if time_ms else "",
        "IP属地": str(comment_data.get("ip_location", "")),
        "点赞数": str(_safe_int(comment_data.get("like_count", 0))),
        "二级评论数": str(_safe_int(comment_data.get("sub_comment_count", 0))),
        "父评论ID": str(parent_id) if is_sub else "",
        "评论图片": str(comment_data.get("pictures", "")),
    }

    return {"fields": fields}


def build_comment_summary(comments: List[Dict[str, Any]], top_n: int = 5) -> str:
    """
    从评论列表中提取热门一级评论，生成摘要文本。

    Args:
        comments: 该笔记下的所有评论
        top_n: 展示前 N 条

    Returns:
        格式化的评论摘要文本
    """
    first_level = []
    for c in comments:
        pid = c.get("parent_comment_id", 0)
        if pid and pid != 0 and str(pid) != "0":
            continue
        content = str(c.get("content", "")).strip()
        if not content:
            continue
        like = 0
        try:
            like = int(c.get("like_count", 0))
        except (ValueError, TypeError):
            pass
        first_level.append((like, content))

    first_level.sort(key=lambda x: x[0], reverse=True)

    lines = []
    for i, (like, content) in enumerate(first_level[:top_n], 1):
        preview = content[:80] + ("..." if len(content) > 80 else "")
        lines.append(f"{i}. [赞{like}] {preview}")

    return "\n".join(lines) if lines else ""
