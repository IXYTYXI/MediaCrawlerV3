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

    def cleanup_default_fields_and_records(self, app_token: str, table_id: str, keep_field_names: set):
        """清理默认字段和空记录"""
        # 删除默认空记录
        try:
            records = self.list_records(app_token, table_id)
            if records:
                record_ids = [r["record_id"] for r in records]
                self.batch_delete_records(app_token, table_id, record_ids)
                utils.logger.info(f"[FeishuBitable] 删除 {len(record_ids)} 条默认空记录")
        except Exception as e:
            utils.logger.warning(f"[FeishuBitable] 删除默认记录失败: {e}")

        # 删除默认字段（不在我们需要的字段列表中的）
        try:
            fields = self.list_fields(app_token, table_id)
            for field in fields:
                if field.get("field_name") not in keep_field_names:
                    try:
                        self.delete_field(app_token, table_id, field["field_id"])
                        utils.logger.info(f"[FeishuBitable] 删除默认字段: {field['field_name']}")
                    except Exception:
                        pass  # 有些系统字段不能删
        except Exception as e:
            utils.logger.warning(f"[FeishuBitable] 删除默认字段失败: {e}")

    def list_tables(self, app_token: str) -> List[Dict]:
        """列出多维表格中的所有数据表"""
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables"
        data = self._request("GET", url)
        return data.get("items", [])

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

    # 动态图片字段
    for i, url in enumerate(image_urls, 1):
        fields[f"图片{i}"] = url

    return {"fields": fields}
