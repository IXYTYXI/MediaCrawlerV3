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
            # 下载图片
            resp = self._client.get(image_url, timeout=30.0, follow_redirects=True)
            if resp.status_code != 200:
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

    # 发布时间：时间戳转日期
    time_val = note_data.get("time", "")
    if isinstance(time_val, (int, float)) and time_val > 0:
        from datetime import datetime
        try:
            time_val = datetime.fromtimestamp(time_val / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            time_val = str(time_val)

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
        "发布时间": str(time_val),
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
