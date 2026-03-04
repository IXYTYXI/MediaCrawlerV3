# 飞书群消息通知卡片 — 技术说明

## 一、功能概述

爬虫任务在以下场景自动向飞书群发送**消息卡片**：

| 场景 | 卡片类型 | 颜色 |
|------|----------|------|
| 任务全部完成 | 任务完成汇总卡片 | 绿色 |
| 任务完成但部分失败 | 任务完成汇总卡片 | 橙色 |
| Session 过期需要登录 | 紧急提醒卡片 | 红色 |

### 汇总卡片内容

- 任务日期、爬取模式（全量/增量/日期范围）、耗时
- ASCII 进度条 `████████░░░░` 和完成百分比
- 四列统计：成功 / 复用 / 失败 / 跳过
- 可点击的「查看飞书表格」按钮（直接跳转多维表格）
- @指定成员 或 @所有人

---

## 二、实现原理

### 发送方式

支持两种方式，**二选一**即可：

#### 方式 A：应用机器人（当前使用）

```
┌─────────────┐     ①获取token     ┌──────────────────┐
│  爬虫服务器   │ ──────────────→  │  飞书开放平台 API   │
│             │     ②发送卡片      │                  │
│  (Python)   │ ──────────────→  │  /im/v1/messages  │
└─────────────┘                  └────────┬─────────┘
                                          │
                                          ▼
                                    ┌───────────┐
                                    │  飞书群聊   │
                                    └───────────┘
```

**流程**：
1. 用 `app_id` + `app_secret` 调用飞书 API 获取 `tenant_access_token`
2. 用 token + `chat_id` 调用消息 API 发送卡片
3. 消息出现在指定群聊中

**所需信息**：
- `app_id` / `app_secret`：飞书开放平台的应用凭证
- `chat_id`：目标群聊的 ID

#### 方式 B：Webhook 自定义机器人

```
┌─────────────┐     POST JSON     ┌──────────────┐
│  爬虫服务器   │ ──────────────→  │  Webhook URL  │ → 飞书群聊
└─────────────┘                  └──────────────┘
```

**流程**：
1. 直接向 Webhook URL 发送 POST 请求
2. 请求体包含消息卡片 JSON

**所需信息**：
- `webhook_url`：群机器人的 Webhook 地址
- `webhook_secret`（可选）：签名校验密钥

### 凭证复用

通知模块的 `app_id` / `app_secret` 如果留空，会自动复用飞书多维表格推送用的同一套凭证（配置文件 `feishu` 节点），无需额外配置。

---

## 三、配置文件

配置位于 `config/anti_crawl_config.json` 的 `notification` 节点：

```json
{
  "notification": {
    "enabled": true,
    "webhook_url": "",
    "webhook_secret": "",
    "chat_id": "oc_f1024adfe4a1db3cda82076de5e9f684",
    "app_id": "",
    "app_secret": "",
    "at_user_ids": ["all"]
  }
}
```

| 字段 | 说明 | 示例值 |
|------|------|--------|
| `enabled` | 是否启用通知 | `true` |
| `webhook_url` | Webhook URL（方式B，留空则用方式A） | `""` |
| `webhook_secret` | Webhook 签名密钥（可选） | `""` |
| `chat_id` | 飞书群聊 ID（方式A） | `"oc_xxxx"` |
| `app_id` | 应用 App ID（留空自动复用飞书推送凭证） | `""` |
| `app_secret` | 应用 App Secret（留空自动复用） | `""` |
| `at_user_ids` | @提醒的用户列表 | `["all"]` = @所有人 |

---

## 四、消息卡片结构

使用[飞书消息卡片](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-components/content-components/rich-text)的 JSON 协议构建，核心结构：

```json
{
  "config": { "wide_screen_mode": true },
  "header": {
    "template": "green",          // 颜色：green/orange/red
    "title": { "tag": "plain_text", "content": "爬取任务完成" }
  },
  "elements": [
    // 1. 信息行：日期 · 模式 · 耗时
    { "tag": "div", "text": { "tag": "lark_md", "content": "..." } },
    // 2. 分割线
    { "tag": "hr" },
    // 3. 进度条
    { "tag": "div", "text": { "tag": "lark_md", "content": "**完成进度** `████░░` **80%**" } },
    // 4. 四列统计面板（column_set）
    { "tag": "column_set", "columns": [ ... ] },
    // 5. 按钮：查看飞书表格
    { "tag": "action", "actions": [{ "tag": "button", "url": "https://..." }] },
    // 6. @提醒
    { "tag": "div", "text": { "tag": "lark_md", "content": "<at id=all></at>" } }
  ]
}
```

### 关键组件说明

| 组件 | 飞书标签 | 作用 |
|------|----------|------|
| 标题栏 | `header.template` | 根据成功/失败自动切换颜色 |
| 富文本 | `lark_md` | 支持加粗、代码块等 Markdown 语法 |
| 多列布局 | `column_set` | 四列并排展示统计数字 |
| 按钮 | `action > button` | 可点击跳转到飞书多维表格 |
| @提醒 | `<at id=all>` | 支持 @所有人 或 @指定用户 |

---

## 五、触发时机

在 `tools/batch_crawler.py` 中的两个位置触发：

### 1. 任务完成通知

```python
# batch_crawler.py 最后阶段
from tools.feishu_notify import send_notification
send_notification(
    task_id=task_id,          # 任务标识
    crawl_mode=crawl_mode,    # full/incremental/date_range
    total=total,              # 总作者数
    success=success,          # 成功数
    failed=failed,            # 失败数
    skipped=skipped,          # 跳过数
    reused=reused,            # 复用数
    elapsed_seconds=elapsed,  # 总耗时
    bitable_url=bitable_url,  # 多维表格链接
    notify_config=config,     # 通知配置
)
```

### 2. Session 过期提醒

```python
# 爬取过程中检测到 Session 过期
from tools.feishu_notify import send_session_expired_alert
send_session_expired_alert(
    creator_name="当前作者名",
    creator_index=3,          # 当前第几个
    total_creators=18,        # 总共几个
    notify_config=config,
)
```

---

## 六、如何获取 chat_id

使用已有应用机器人的凭证，调用飞书 API：

```bash
# 1. 获取 token
curl -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H "Content-Type: application/json" \
  -d '{"app_id": "你的app_id", "app_secret": "你的app_secret"}'

# 2. 列出机器人所在的群聊
curl https://open.feishu.cn/open-apis/im/v1/chats \
  -H "Authorization: Bearer 上一步获取的token"
```

返回的 `chat_id`（以 `oc_` 开头）填入配置文件即可。

**前提条件**：应用机器人需要：
1. 在飞书开放平台开启 `im:chat:readonly` 权限（读取群信息）
2. 在飞书开放平台开启 `im:message:send_as_bot` 权限（发送消息）
3. 已被添加到目标群聊中

---

## 七、代码文件

| 文件 | 职责 |
|------|------|
| `tools/feishu_notify.py` | 通知模块：构建卡片 JSON、发送 Webhook / 应用机器人消息 |
| `config/anti_crawl_config.json` → `notification` | 配置：开关、凭证、群 ID、@对象 |
| `tools/batch_crawler.py` | 调用方：任务完成和 Session 过期时触发通知 |
