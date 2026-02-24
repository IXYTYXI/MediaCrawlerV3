# MediaCrawler V3 — 项目部署与开发全记录

> 本文档是基于 [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 的二次开发版本的完整部署指南和开发历程记录。
> 目标：**小红书创作者内容批量爬取 → 飞书多维表格自动上传**，附带 Web 控制面板。

---

## 目录

- [一、项目概览](#一项目概览)
- [二、环境准备](#二环境准备)
- [三、安装部署（从零开始）](#三安装部署从零开始)
- [四、配置说明](#四配置说明)
- [五、使用指南](#五使用指南)
- [六、开发历程与踩坑记录](#六开发历程与踩坑记录)
- [七、架构说明](#七架构说明)
- [八、常见问题 FAQ](#八常见问题-faq)

---

## 一、项目概览

### 做了什么

在原版 MediaCrawler 基础上，增加了：

1. **Web Dashboard 控制面板** — 在浏览器上管理爬虫，无需 SSH
2. **批量爬取调度器** — 读取 Excel 中的作者列表，自动逐个爬取
3. **断点续爬** — 支持中断后从上次位置继续，不丢数据
4. **优雅中止** — SIGTERM 信号触发数据保存，不会白爬
5. **飞书集成** — 爬取结果自动推送到飞书多维表格（含图片/视频上传）
6. **密码保护** — Dashboard 增加登录验证，防止外部扫描
7. **反爬策略** — 随机等待、批次暂停、动态调速、假动作模拟
8. **Session 管理** — 扫码登录、Cookie 更新、Session 有效性验证

### 技术栈

| 组件 | 技术 |
|------|------|
| 后端 API | Python 3.10+ / FastAPI / Uvicorn |
| 前端面板 | Vue 3 (CDN) + Tailwind CSS |
| 爬虫引擎 | Playwright (Chromium) |
| 数据存储 | JSON 文件 / Excel / 飞书多维表格 |
| 环境管理 | Conda (uvenv 环境) |
| 进程管理 | nohup + asyncio subprocess |

---

## 二、环境准备

### 系统要求

- **操作系统**: Linux (推荐 Ubuntu 20.04+)
- **Python**: 3.10 或以上
- **内存**: 至少 4GB（Playwright 浏览器占内存）
- **磁盘**: 至少 10GB 剩余（图片/视频缓存）

### 前置软件

```bash
# 1. 安装 Anaconda/Miniconda（如已有则跳过）
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh

# 2. 创建 conda 环境
conda create -n uvenv python=3.10 -y
conda activate uvenv
```

---

## 三、安装部署（从零开始）

### 步骤 1：克隆代码

```bash
git clone https://github.com/IXYTYXI/MediaCrawlerV3.git
cd MediaCrawlerV3
git checkout feature/video-upload
```

### 步骤 2：安装 Python 依赖

```bash
conda activate uvenv

# 安装项目依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium

# 安装系统级依赖（Playwright 需要）
playwright install-deps
```

### 步骤 3：准备 Excel 作者列表

在项目根目录放置 `redbookaccontidandresult.xlsx`，格式要求：

| 列名 | 说明 | 示例 |
|------|------|------|
| 小红书链接 | 作者主页完整 URL | `https://www.xiaohongshu.com/user/profile/xxx?xsec_token=...` |

> 链接必须包含 `xsec_token` 参数，可以从浏览器地址栏复制。

### 步骤 4：配置反爬策略

复制配置模板并修改：

```bash
# 如果 config/anti_crawl_config.json 不存在，首次启动会自动生成默认配置
# 也可以通过 Dashboard 界面直接修改
```

关键配置项（`config/anti_crawl_config.json`）：

```json
{
  "crawl_settings": {
    "platform": "xhs",
    "crawler_type": "creator",
    "max_notes_count": 3000
  },
  "batch_crawl": {
    "enabled": true,
    "excel_path": "redbookaccontidandresult.xlsx",
    "max_notes_per_creator": 3000,
    "resume": true
  }
}
```

### 步骤 5：配置登录信息

编辑 `config/base_config.py`：

```python
# 登录方式：cookie（推荐）或 qrcode
LOGIN_TYPE = "cookie"

# 如果用 cookie 方式，填入 web_session 值
COOKIES = "your_web_session_value_here"
```

> 首次推荐通过 Dashboard 的「扫码登录」功能获取 Session。

### 步骤 6：设置 Dashboard 密码

```bash
# 设置环境变量（写入 ~/.bashrc 持久化）
echo 'export MC_DASHBOARD_PWD="你的复杂密码"' >> ~/.bashrc
source ~/.bashrc
```

### 步骤 7：启动 API 服务

```bash
cd /data/vonjan/program/MediaCrawler

MC_DASHBOARD_PWD="你的复杂密码" nohup conda run --no-capture-output -n uvenv \
  python -m uvicorn api.main:app --host 0.0.0.0 --port 9001 \
  > api.log 2>&1 &

echo $!  # 记录 PID
```

### 步骤 8：验证

打开浏览器访问 `http://你的服务器IP:9001/dashboard`

- 应该看到登录界面
- 输入密码后进入控制面板
- 左侧菜单：登录管理 → 爬取设置 → 反爬策略 → 批量爬取 → 飞书配置 → 断点续爬 → 日志/文件

---

## 四、配置说明

### 4.1 反爬策略配置

所有策略可在 Dashboard「反爬策略」标签页可视化调整：

| 策略 | 作用 | 建议值 |
|------|------|--------|
| 随机等待 | 每次请求后随机等待 | 5-10 秒，对数正态分布 |
| 批次暂停 | 每 16-30 条笔记暂停 | 30-60 秒 |
| 动态调速 | 连续成功加速，遇阻减速 | 默认即可 |
| 假动作 | 模拟人类操作（滚动、鼠标移动） | 概率 15% |
| 超级休眠 | 检测到封锁时长时间等待 | 300-600 秒 |

### 4.2 批量爬取参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| limit | 爬取作者数量限制（0=全部） | 0 |
| min_interaction | 最低互动量过滤 | 0（不过滤） |
| enable_comments | 是否爬取评论 | false |
| max_notes_per_creator | 每个作者最多爬取笔记数 | 3000 |

### 4.3 日期截止

在 `batch_crawler.py` 中配置：
- `date_cutoff`: 连续 N 条笔记早于截止日期时自动跳过该作者
- 默认：连续 5 条早于 2025-01-01 时停止

### 4.4 飞书集成

在 Dashboard「飞书配置」填入：
- App ID / App Secret（飞书开放平台创建应用获取）
- Folder Token（飞书知识库文件夹 Token）

---

## 五、使用指南

### 5.1 日常操作流程

```
1. 打开 Dashboard → 输入密码登录
2. 检查「登录管理」→ Session 状态是否有效
3. （如果无效）点击「扫码登录」→ 手机扫码
4. 切换到「批量爬取」→ 确认作者列表
5. 点击「开始批量爬取」
6. 在「日志/文件」标签页实时观察进度
7. 爬取完成后，点击「导出 Excel」
8. （可选）配置飞书后自动上传到多维表格
```

### 5.2 中断与恢复

**优雅中止（推荐）：**
- 点击 Dashboard 的「优雅中止」按钮
- 系统会保存当前作者的已爬数据，标记为 partial
- 下次启动自动补爬未完成的作者

**强制中断（Ctrl+C / kill）：**
- 已实现 SIGTERM 信号处理
- 同样会尝试保存部分数据

**恢复爬取：**
- 直接再次点击「开始批量爬取」
- 已完成的作者自动跳过
- partial 状态的作者会重新爬取（旧数据合并保留）

### 5.3 API 服务管理

```bash
# 查看日志
tail -f /data/vonjan/program/MediaCrawler/api.log

# 重启服务
kill $(pgrep -f "uvicorn api.main") 2>/dev/null
sleep 2
MC_DASHBOARD_PWD="你的密码" nohup conda run --no-capture-output -n uvenv \
  python -m uvicorn api.main:app --host 0.0.0.0 --port 9001 \
  > api.log 2>&1 &

# 查看进程
ps aux | grep uvicorn
```

### 5.4 命令行直接爬取（不经过 Dashboard）

```bash
conda activate uvenv
cd /data/vonjan/program/MediaCrawler

# 爬取全部作者
python -m tools.batch_crawler --skip-feishu --min-interaction 0 --max-notes 3000

# 仅导出已有数据为 Excel
python -m tools.batch_crawler --export-only --export-format excel
```

---

## 六、开发历程与踩坑记录

> 以下按时间顺序记录了项目开发过程中遇到的每一个问题，包括起因、排查过程和最终解决方案。

### 问题 1：`uv not found` — 命令执行环境错误

**起因：**
原版 MediaCrawler 使用 `uv` 作为 Python 环境管理工具。但我们的服务器用的是 Conda，环境名为 `uvenv`。代码中有 4 处硬编码了 `uv run` 命令。

**发现过程：**
在 Dashboard 点击「开始爬取」后，API 返回错误，日志显示 `uv: command not found`。

**尝试的方案：**
1. 先尝试 `pip install uv` — 失败，因为服务器网络受限
2. 确认服务器已有 conda 环境 `uvenv`

**最终修复：**
将所有 `"uv", "run"` 替换为 `"conda", "run", "--no-capture-output", "-n", "uvenv", "python", "-u"`：

| 文件 | 行数 | 改动 |
|------|------|------|
| `api/main.py` | ~119 | 环境检查命令 |
| `api/services/crawler_manager.py` | ~207 | 单次爬取命令 |
| `api/routers/dashboard.py` | ~133 | 批量爬取命令 |
| `api/routers/dashboard.py` | ~276 | 导出命令 |

**教训：** 部署时一定要确认实际的 Python 环境管理工具，不能假设 `uv` 可用。

---

### 问题 2：API 返回中文乱码

**起因：**
FastAPI 默认的 `JSONResponse` 不设 `charset=utf-8`，浏览器有时按 GBK 解码。

**表现：**
API 返回 `{"output": "鍛戒护琛屽叆鍙"}` 而不是 `{"output": "命令行入口"}`。

**修复：**
在 `api/main.py` 中创建自定义响应类：

```python
class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

app = FastAPI(default_response_class=UTF8JSONResponse)
```

同时在所有 `subprocess` 调用中设置环境变量 `PYTHONIOENCODING=utf-8`。

**教训：** 涉及中文的 Web 服务，永远显式设置 `charset=utf-8`。

---

### 问题 3：Dashboard 只爬了 5 个作者（实际有 18 个）

**起因：**
前端 `dashboard/index.html` 中 `batchForm` 默认值写死了 `limit: 5`，意味着每次只提交前 5 个作者。

**发现过程：**
用户发现 Excel 有 18 个作者，但日志只显示处理了 5 个。逐层排查后端 API → 发现是前端传参问题。

**修复：**
`dashboard/index.html` 中将 `batchForm.limit` 从 `5` 改为 `0`（0 代表不限制）。

**教训：** 前端默认值要慎重，尤其是会影响数据量的参数。

---

### 问题 4：9 个作者爬取失败（Session 过期）

**起因：**
在一次长时间爬取过程中（持续数小时），小红书的登录 Session 过期了。前 9 个作者爬取成功，后 9 个全部失败。

**表现：**
进度文件 `batch_progress_task_20260210.json` 中有 9 条 `failed` 记录，message 为 "Session 过期导致未爬取"。

**问题：**
失败记录会阻止这些作者被重新爬取（系统认为"已处理"），用户无法通过界面清除。

**修复：**
1. 后端增加 `POST /batch/progress/clear-failed` 端点
2. 前端增加「清除失败记录」按钮
3. 增加任务状态面板，显示 completed / failed / partial / remaining 计数

**教训：** 批量任务必须提供失败重试机制，不能让失败记录变成"死锁"。

---

### 问题 5：`_get_task_summary` 导入错误

**起因：**
在 `dashboard.py` 中添加了 task-summary 端点，但函数定义因为编辑工具的问题没有正确写入。

**修复：**
重新正确添加 `_get_task_summary()` 函数定义和对应的 API 端点。

---

### 问题 6：`dashboard.py` 缩进错误

**起因：**
在添加 WebSocket 日志推送功能时，模块级变量 `_ws_clients: set = set()` 被错误地放在了函数内部，导致 `IndentationError`。

**修复：**
将 `_ws_clients` 移到模块顶层（函数外部）。

**教训：** Python 对缩进敏感，自动化编辑工具需要仔细检查缩进层级。

---

### 问题 7：日志窗口不刷新

**起因：**
最初用 5 秒轮询 (`setInterval`) 获取日志，但 API 端的日志读取存在竞态条件，经常返回空或重复内容。

**用户反馈：**
"半小时没刷新日志了"

**修复方案：**
用 WebSocket 替代轮询：

1. 后端：`api/routers/dashboard.py` 增加 `/batch/logs/ws` WebSocket 端点
2. 每条日志即时广播给所有连接的客户端
3. 前端：`dashboard/index.html` 改用 WebSocket 连接，实时显示
4. 增加「实时」指示器（绿色脉冲圆点）和「重连」按钮

```
之前：前端 → 5秒轮询 GET /logs → 后端返回缓存日志（经常延迟或丢失）
之后：后端 → 每行日志即时 WebSocket 推送 → 前端实时渲染
```

**教训：** 需要实时反馈的场景，WebSocket 远优于轮询。

---

### 问题 8：中断爬取导致数据丢失

**起因：**
爬虫按作者逐个爬取，只有完整爬完一个作者后才写入任务目录。如果中途 kill 进程，当前作者已爬的几百条笔记全部丢失。

**真实场景：**
用户在爬第 10 个作者时（已爬 265 条笔记，耗时约 1 小时），需要重启服务。Ctrl+C 后这 265 条数据全部消失。

**修复（分两步实施）：**

**第一步：临时手动保存脚本**
- 创建 `save_and_mark.py`，手动将当前 raw 数据 snapshot 并写入任务目录
- 用户先运行脚本保存，再 kill 进程

**第二步：自动优雅中止机制**

1. `tools/batch_crawler.py` 增加 `_GracefulShutdown` 类：
   - 注册 SIGTERM / SIGINT 信号处理器
   - 收到信号时：收集当前作者的部分数据 → 写入任务目录 → 标记为 partial → 退出

2. `BatchProgress` 类增加 `partial` 状态：
   - `mark_partial(url, info)` — 标记作者为部分完成
   - 下次启动时检测到 partial 状态 → 重新爬取（旧数据作为 reuse 合并）

3. Dashboard 增加「优雅中止」按钮：
   - 发送 SIGTERM → 等待最多 30 秒 → 如超时则 SIGKILL
   - 显示确认对话框和进度 spinner

**教训：** 长时间运行的数据采集任务，必须支持中断恢复。"永远假设用户会在任何时候中断"。

---

### 问题 9：公网暴露 + 外部机器人扫描

**起因：**
API 监听 `0.0.0.0:9001`，服务器有公网 IP，被互联网上的自动扫描机器人探测到。

**日志表现：**
```
INFO: 18.116.101.220:53078 - "GET / HTTP/1.1" 200 OK
INFO: 167.94.138.163:60810 - "GET / HTTP/1.1" 200 OK
WARNING: Invalid HTTP request received.
INFO: 167.94.138.163:60836 - "GET /vite.svg HTTP/1.1" 404
INFO: 167.94.138.163:53488 - "GET /wiki HTTP/1.1" 404
```

这些 IP（18.116.x.x 是 AWS，167.94.x.x 是 Censys 扫描器）属于互联网背景噪声。

**评估的方案：**

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| IP 白名单 | 最安全 | 用户是动态 WiFi IP，经常变化 | 固定 IP 场景 |
| SSH 隧道 | 非常安全 | 每次使用要开隧道 | 技术用户 |
| 密码认证 | 简单有效 | 密码可能被暴力破解 | 大多数场景 |

**最终选择：密码认证（方案 3）**

实现：
1. `api/main.py` — 中间件拦截所有请求，检查 Bearer Token
2. `/api/auth/login` — 密码验证端点（SHA-256 哈希比对）
3. 密码从环境变量 `MC_DASHBOARD_PWD` 读取，不在代码中硬编码
4. Token 存在服务端内存 `set` 中，API 重启后需重新登录
5. 前端 — 全屏登录遮罩，Token 存 `localStorage`

```python
# 公开路径（不需要验证）
_PUBLIC_PATHS = {"/", "/api/health", "/api/auth/login", "/api/auth/check", "/favicon.ico"}

# 中间件逻辑
async def auth_middleware(request, call_next):
    if path in _PUBLIC_PATHS → 放行
    if 静态资源 (.js/.css/.png) → 放行
    if WebSocket → 从 query 参数取 token
    if HTTP API → 从 Authorization header 取 token
    if HTML 页面请求 → 放行（前端自己判断登录状态）
    否则 → 返回 401
```

**教训：** 任何暴露在公网的服务都必须有认证。即使是"内部工具"，也会在几小时内被全网扫描器发现。

---

## 七、架构说明

### 目录结构

```
MediaCrawler/
├── api/                          # FastAPI 后端
│   ├── main.py                   # 应用入口 + 密码中间件
│   ├── control.html              # 控制面板前端
│   ├── login.html                # 扫码登录前端
│   ├── routers/
│   │   ├── dashboard.py          # Dashboard API（配置/爬取/导出/Session/飞书）
│   │   ├── control.py            # 控制面板 API（启停/日志/进度）
│   │   ├── login.py              # 远程登录 API（Playwright 扫码）
│   │   ├── crawler.py            # 原版爬虫 API
│   │   └── data.py               # 数据查询 API
│   └── services/
│       └── crawler_manager.py    # 爬虫进程管理
│
├── dashboard/
│   └── index.html                # Dashboard 前端（Vue 3 单文件）
│
├── tools/
│   ├── batch_crawler.py          # 批量爬取调度器（核心）
│   ├── crawl_progress.py         # 笔记级断点续爬
│   ├── async_file_writer.py      # 异步文件写入器
│   ├── excel_reader.py           # Excel 作者列表读取
│   ├── feishu_bitable.py         # 飞书多维表格操作
│   ├── video_script_extractor.py # AI 视频脚本提取
│   └── session_keeper.py         # Session 保活
│
├── media_platform/xhs/
│   ├── core.py                   # 小红书爬虫核心逻辑
│   ├── client.py                 # API 客户端
│   └── exception.py              # 自定义异常
│
├── config/
│   ├── base_config.py            # 基础配置（登录/Cookie/平台）
│   └── anti_crawl_config.json    # 反爬策略配置（通过 Dashboard 管理）
│
└── data/
    └── xhs/json/
        ├── task_20260210/        # 任务数据目录（按 task_id 命名）
        │   ├── creator_xxx_contents.json   # 每个作者的完整数据
        │   └── creator_xxx_reuse.json      # 复用的历史数据
        ├── creator_contents_*.json          # 实时写入的 raw 数据
        └── batch_progress_*.json            # 批量爬取进度文件
```

### 数据流

```
Excel 作者列表
    ↓
batch_crawler.py（读取 Excel → 逐个调度）
    ↓
core.py（Playwright 打开浏览器 → 爬取笔记列表 → 获取详情）
    ↓
async_file_writer.py（实时写入 raw JSON）
    ↓
batch_crawler.py（爬完后收集数据 → 写入 task 目录 → 更新 progress）
    ↓
export / feishu upload（按需导出 Excel 或上传飞书）
```

### API 路由映射

| 路径 | 功能 |
|------|------|
| `GET /dashboard` | Dashboard 前端页面 |
| `POST /api/auth/login` | 密码登录 |
| `GET /api/auth/check` | Token 验证 |
| `GET /api/dashboard/config` | 获取配置 |
| `PUT /api/dashboard/config` | 更新配置 |
| `POST /api/dashboard/batch/start` | 启动批量爬取 |
| `POST /api/dashboard/batch/stop` | 停止爬取 |
| `GET /api/dashboard/batch/status` | 爬取状态 |
| `GET /api/dashboard/batch/logs` | 爬取日志 |
| `WS /dashboard/batch/logs/ws` | 日志实时推送 |
| `GET /api/dashboard/task-summary` | 任务统计 |
| `POST /api/dashboard/batch/progress/clear-failed` | 清除失败记录 |
| `GET /api/dashboard/files` | 数据文件列表 |
| `GET /api/dashboard/files/view` | 预览文件内容 |
| `GET /api/dashboard/session/status` | Session 状态 |
| `POST /api/dashboard/session/verify` | 验证 Session |
| `POST /api/dashboard/session/cookie` | 更新 Cookie |
| `POST /api/dashboard/export` | 触发导出 |

---

## 八、常见问题 FAQ

### Q: 启动后访问 Dashboard 白屏？
A: 检查 `dashboard/index.html` 是否存在。确认访问的是 `http://IP:9001/dashboard`（注意末尾没有斜杠）。

### Q: 密码忘了怎么办？
A: 重新设置环境变量并重启：
```bash
export MC_DASHBOARD_PWD="新密码"
kill $(pgrep -f "uvicorn api.main"); sleep 2
# 然后重新启动
```

### Q: 爬取速度很慢？
A: 正常情况。反爬策略会在每次请求间插入 5-10 秒等待，每 20-30 条还会暂停 30-60 秒。一个有 400+ 笔记的作者可能需要 1-2 小时。

### Q: Session 过期了怎么办？
A: 在 Dashboard「登录管理」中点击「扫码登录」，用手机小红书扫码。或者手动更新 Cookie。

### Q: 爬到一半服务器断电了，数据还在吗？
A: 如果是通过「优雅中止」停止的 → 数据已保存。如果是突然断电 → 已完成的作者数据不受影响，当前正在爬的作者数据可能丢失（但下次会重新爬）。

### Q: 如何查看哪些作者已经爬完了？
A: 在 Dashboard「批量爬取」标签页查看任务状态面板。或直接查看进度文件：
```bash
cat data/batch_progress_task_*.json | python3 -m json.tool
```

### Q: 如何只导出已爬取的数据（不重新爬取）？
A:
```bash
conda activate uvenv
python -m tools.batch_crawler --export-only --export-format excel
```

### Q: 如何修改日期截止范围？
A: 目前在 `tools/batch_crawler.py` 的 `run_batch_crawl` 函数中硬编码。搜索 `date_cutoff` 或 `2025-01-01` 进行修改。

### Q: 远程 IP 变了怎么办？
A: 密码认证不依赖 IP，换 WiFi / 换网络后只需重新登录即可。

---

## 附录

### 重启命令速查

```bash
# 停止
kill $(pgrep -f "uvicorn api.main") 2>/dev/null

# 启动
cd /data/vonjan/program/MediaCrawler
MC_DASHBOARD_PWD="你的密码" nohup conda run --no-capture-output -n uvenv \
  python -m uvicorn api.main:app --host 0.0.0.0 --port 9001 \
  > api.log 2>&1 &

# 查看日志
tail -f api.log

# 查看爬虫进度
cat data/batch_progress_task_*.json | python3 -m json.tool
```

### 版本信息

- 基于 MediaCrawler 原版 fork
- 分支: `feature/video-upload`
- 最后更新: 2026-02-24
