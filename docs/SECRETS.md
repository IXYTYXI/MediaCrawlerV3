# 密钥与凭证配置说明

> **重要提示**：本文件记录项目中所有需要配置的密码、密钥和凭证。
> 仓库设为私密后方可安全保存。如果仓库是公开的，请勿在此文件中填写真实值。

---

## 1. Dashboard 面板登录密码

| 项目 | 说明 |
|------|------|
| **环境变量** | `MC_DASHBOARD_PWD` |
| **配置文件** | `api/main.py` 第 59 行 |
| **用途** | Web 控制面板的访问密码，浏览器打开面板时需要输入 |
| **默认值** | `changeme`（必须修改） |

设置方式 - 启动 API 前在终端 export：

```bash
export MC_DASHBOARD_PWD="你的复杂密码"
```

启动命令示例：

```bash
MC_DASHBOARD_PWD="你的密码" nohup conda run --no-capture-output -n uvenv \
  python -m uvicorn api.main:app --host 0.0.0.0 --port 9001 > api.log 2>&1 &
```

---

## 2. 飞书开放平台凭证

| 项目 | 当前值 | 说明 |
|------|--------|------|
| **App ID** | `cli_a9006c0b96395bd3` | 飞书开放平台 App ID |
| **App Secret** | `tul9PNh3aSjIbcqcoB25vh8HLYP2HmPx` | 飞书开放平台 App Secret |
| **Folder Token** | `LLTwf1fRUl3tH9dxr2PcMqplnAh` | 飞书云文档文件夹 token，数据上传到此文件夹 |

**配置文件**：`config/anti_crawl_config.json` 中的 `feishu_config` 节点

```json
{
  "feishu_config": {
    "app_id": "cli_a9006c0b96395bd3",
    "app_secret": "tul9PNh3aSjIbcqcoB25vh8HLYP2HmPx",
    "folder_token": "LLTwf1fRUl3tH9dxr2PcMqplnAh"
  }
}
```

**用途**：
- 爬取的数据通过飞书 API 上传到多维表格
- 视频/图片文件通过飞书文件上传 API 存储
- Dashboard 面板的「飞书上传」和「视频脚本提取」功能依赖此凭证

**获取方式**：
1. 登录 [飞书开放平台](https://open.feishu.cn/)
2. 创建企业自建应用
3. 在「凭证与基础信息」页面获取 App ID 和 App Secret
4. 给应用开通权限：`bitable:app`、`drive:drive`、`drive:file`
5. Folder Token 从飞书云文档的文件夹 URL 中提取（URL 末尾那段字符串）

---

## 3. Gemini API Key（视频脚本提取用）

| 项目 | 说明 |
|------|------|
| **配置方式** | Dashboard 面板 - 视频脚本提取 - API Key 输入框 |
| **用途** | 调用 Google Gemini API 分析视频内容并提取脚本文案 |
| **存储** | 仅在浏览器前端内存中，不持久化到服务端 |

**获取方式**：
1. 访问 [Google AI Studio](https://aistudio.google.com/)
2. 创建 API Key
3. 在 Dashboard 使用时直接粘贴即可

---

## 4. 小红书登录态 Cookie

| 项目 | 说明 |
|------|------|
| **配置文件** | `config/base_config.py` 第 27 行 `COOKIES` |
| **自动保存位置** | `data/cookies/xhs_cookies.json` |
| **用途** | 小红书平台的登录态，爬虫运行时需要 |

**获取方式**：
- **方式一（推荐）**：通过 Dashboard 面板的「扫码登录」功能自动获取，cookie 自动保存
- **方式二**：浏览器手动登录小红书，F12 开发者工具 - Application - Cookies - 复制 `web_session` 的值

> Cookie 有效期有限，过期后需要重新登录。
> 正常使用时爬虫优先读取 `data/cookies/` 下自动保存的 cookie 文件。

---

## 5. 数据库凭证（可选，默认不使用）

项目默认使用 JSON 文件存储数据，以下数据库配置仅在切换存储方式时需要。

**配置文件**：`.env`（项目根目录）和 `config/db_config.py`

### MySQL

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MYSQL_DB_USER` | `root` | 用户名 |
| `MYSQL_DB_PWD` | `123456` | 密码 |
| `MYSQL_DB_HOST` | `localhost` | 主机 |
| `MYSQL_DB_PORT` | `3306` | 端口 |
| `MYSQL_DB_NAME` | `media_crawler` | 数据库名 |

### Redis（登录态缓存用）

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `REDIS_DB_HOST` | `127.0.0.1` | 主机 |
| `REDIS_DB_PWD` | `123456` | 密码 |
| `REDIS_DB_PORT` | `6379` | 端口 |

### PostgreSQL

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `POSTGRES_DB_USER` | `postgres` | 用户名 |
| `POSTGRES_DB_PWD` | `123456` | 密码 |
| `POSTGRES_DB_HOST` | `localhost` | 主机 |
| `POSTGRES_DB_PORT` | `5432` | 端口 |
| `POSTGRES_DB_NAME` | `media_crawler` | 数据库名 |

### MongoDB

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MONGODB_HOST` | `localhost` | 主机 |
| `MONGODB_PORT` | `27017` | 端口 |
| `MONGODB_USER` | （空） | 用户名 |
| `MONGODB_PWD` | （空） | 密码 |
| `MONGODB_DB_NAME` | `media_crawler` | 数据库名 |

---

## 6. 代理服务凭证（可选）

不使用 IP 代理时无需配置。配置文件：`.env`

### 豌豆 HTTP 代理

| 环境变量 | 说明 |
|---------|------|
| `WANDOU_APP_KEY` | 豌豆 HTTP 的 App Key |

### 快代理

| 环境变量 | 说明 |
|---------|------|
| `KDL_SECERT_ID` | Secret ID |
| `KDL_SIGNATURE` | 签名 |
| `KDL_USER_NAME` | 用户名 |
| `KDL_USER_PWD` | 密码 |

### 极速 HTTP 代理

| 环境变量 | 说明 |
|---------|------|
| `jisu_key` | 提取 Key |
| `jisu_crypto` | 加密签名 |

---

## 7. 小红书 xsec_token（创作者爬取用）

| 项目 | 说明 |
|------|------|
| **配置文件** | `config/anti_crawl_config.json` 中 `creator_ids` 数组 |
| **用途** | 爬取指定创作者主页时 URL 中必须携带的 token |

**获取方式**：
1. 浏览器登录小红书
2. 搜索并进入目标创作者主页
3. 从地址栏复制完整 URL（包含 `xsec_token=...` 参数）
4. 粘贴到配置文件的 `creator_ids` 数组中

> xsec_token 有时效性，过期后需重新获取。

---

## 快速检查清单

部署时确认以下项目已正确配置：

- [ ] `MC_DASHBOARD_PWD` 环境变量已设置（面板密码）
- [ ] `config/anti_crawl_config.json` 中飞书凭证已填写（如需飞书上传）
- [ ] 小红书 Cookie 已通过面板扫码获取（或手动填写）
- [ ] `.env` 中数据库密码已修改（如使用数据库存储）
- [ ] 代理服务 Key 已配置（如使用 IP 代理）
