# 🔥 MediaCrawler - 自媒体平台爬虫 🕷️

<div align="center">

[![GitHub Stars](https://img.shields.io/github/stars/NanmiCoder/MediaCrawler?style=social)](https://github.com/NanmiCoder/MediaCrawler/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/NanmiCoder/MediaCrawler?style=social)](https://github.com/NanmiCoder/MediaCrawler/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/NanmiCoder/MediaCrawler)](https://github.com/NanmiCoder/MediaCrawler/issues)
[![GitHub Pull Requests](https://img.shields.io/github/issues-pr/NanmiCoder/MediaCrawler)](https://github.com/NanmiCoder/MediaCrawler/pulls)
[![License](https://img.shields.io/github/license/NanmiCoder/MediaCrawler)](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)
[![中文](https://img.shields.io/badge/🇨🇳_中文-当前-blue)](README.md)
[![English](https://img.shields.io/badge/🇺🇸_English-Available-green)](README_en.md)
[![Español](https://img.shields.io/badge/🇪🇸_Español-Available-green)](README_es.md)
</div>



> **免责声明：**
> 
> 大家请以学习为目的使用本仓库⚠️⚠️⚠️⚠️，[爬虫违法违规的案件](https://github.com/HiddenStrawberry/Crawler_Illegal_Cases_In_China)  <br>
>
>本仓库的所有内容仅供学习和参考之用，禁止用于商业用途。任何人或组织不得将本仓库的内容用于非法用途或侵犯他人合法权益。本仓库所涉及的爬虫技术仅用于学习和研究，不得用于对其他平台进行大规模爬虫或其他非法行为。对于因使用本仓库内容而引起的任何法律责任，本仓库不承担任何责任。使用本仓库的内容即表示您同意本免责声明的所有条款和条件。
>
> 点击查看更为详细的免责声明。[点击跳转](#disclaimer)




## 📖 项目简介

一个功能强大的**多平台自媒体数据采集工具**，支持小红书、抖音、快手、B站、微博、贴吧、知乎等主流平台的公开信息抓取。

### 🔧 技术原理

- **核心技术**：基于 [Playwright](https://playwright.dev/) 浏览器自动化框架登录保存登录态
- **无需JS逆向**：利用保留登录态的浏览器上下文环境，通过 JS 表达式获取签名参数
- **优势特点**：无需逆向复杂的加密算法，大幅降低技术门槛


## ✨ 功能特性
| 平台   | 关键词搜索 | 指定帖子ID爬取 | 二级评论 | 指定创作者主页 | 登录态缓存 | IP代理池 | 生成评论词云图 |
| ------ | ---------- | -------------- | -------- | -------------- | ---------- | -------- | -------------- |
| 小红书 | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |
| 抖音   | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |
| 快手   | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |
| B 站   | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |
| 微博   | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |
| 贴吧   | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |
| 知乎   | ✅          | ✅              | ✅        | ✅              | ✅          | ✅        | ✅              |



## 🚀 快速开始

## 📋 前置依赖

### 🚀 uv 安装（推荐）

在进行下一步操作之前，请确保电脑上已经安装了 uv：

- **安装地址**：[uv 官方安装指南](https://docs.astral.sh/uv/getting-started/installation)
- **验证安装**：终端输入命令 `uv --version`，如果正常显示版本号，证明已经安装成功
- **推荐理由**：uv 是目前最强的 Python 包管理工具，速度快、依赖解析准确

### 🟢 Node.js 安装

项目依赖 Node.js，请前往官网下载安装：

- **下载地址**：https://nodejs.org/en/download/
- **版本要求**：>= 16.0.0

### 📦 Python 包安装

```shell
# 进入项目目录
cd MediaCrawler

# 使用 uv sync 命令来保证 python 版本和相关依赖包的一致性
uv sync
```

### 🌐 浏览器驱动安装

```shell
# 安装浏览器驱动
uv run playwright install
```

## 🚀 运行爬虫程序

```shell
# 在 config/base_config.py 查看配置项目功能，写的有中文注释

# 从配置文件中读取关键词搜索相关的帖子并爬取帖子信息与评论
uv run main.py --platform xhs --lt qrcode --type search

# 从配置文件中读取指定的帖子ID列表获取指定帖子的信息与评论信息
uv run main.py --platform xhs --lt qrcode --type detail

# 打开对应APP扫二维码登录

# 其他平台爬虫使用示例，执行下面的命令查看
uv run main.py --help
```

## 🧭 如何控制爬取内容（配置速查）

> 这一节专门告诉你：**爬什么、爬多少、保存哪些字段**分别改哪里，自己就能完全掌控。

### 1) 入口配置：默认用配置文件，命令行可覆盖

- 默认配置在 `config/base_config.py`，运行时可被命令行覆盖（见 `cmd_arg/arg.py` 的覆盖逻辑）。
- 常用命令行覆盖参数：
  - `--platform`：平台
  - `--type`：爬取类型（`search` | `detail` | `creator`）
  - `--keywords`：关键词（search 模式）
  - `--specified_id`：指定帖子/视频列表（detail 模式，仅支持 xhs/bili/dy/wb/ks）
  - `--creator_id`：指定作者列表（creator 模式，仅支持 xhs/bili/dy/wb/ks）
  - `--start`：起始页
  - `--get_comment` / `--get_sub_comment`：评论开关
  - `--max_comments_count_singlenotes`：单条评论数量上限
  - `--save_data_option`：保存类型
  - `--headless`：无头模式

### 2) 选择“爬什么内容”

- **关键词搜索（search）**
  - `config/base_config.py`
    - `CRAWLER_TYPE = "search"`
    - `KEYWORDS = "关键词1,关键词2"`

- **指定帖子/视频（detail）**
  - `config/base_config.py`：`CRAWLER_TYPE = "detail"`
  - 各平台列表（写在对应配置文件里）：
    - 小红书：`config/xhs_config.py` → `XHS_SPECIFIED_NOTE_URL_LIST`（必须带 `xsec_token`）
    - 抖音：`config/dy_config.py` → `DY_SPECIFIED_ID_LIST`
    - 快手：`config/ks_config.py` → `KS_SPECIFIED_ID_LIST`
    - B站：`config/bilibili_config.py` → `BILI_SPECIFIED_ID_LIST`
    - 微博：`config/weibo_config.py` → `WEIBO_SPECIFIED_ID_LIST`
    - 贴吧：`config/tieba_config.py` → `TIEBA_SPECIFIED_ID_LIST`
    - 知乎：`config/zhihu_config.py` → `ZHIHU_SPECIFIED_ID_LIST`

- **指定作者主页（creator）**
  - `config/base_config.py`：`CRAWLER_TYPE = "creator"`
  - 各平台列表：
    - 小红书：`config/xhs_config.py` → `XHS_CREATOR_ID_LIST`（需带 `xsec_token`）
    - 抖音：`config/dy_config.py` → `DY_CREATOR_ID_LIST`
    - 快手：`config/ks_config.py` → `KS_CREATOR_ID_LIST`
    - B站：`config/bilibili_config.py` → `BILI_CREATOR_ID_LIST`
    - 微博：`config/weibo_config.py` → `WEIBO_CREATOR_ID_LIST`
    - 贴吧：`config/tieba_config.py` → `TIEBA_CREATOR_URL_LIST`
    - 知乎：`config/zhihu_config.py` → `ZHIHU_CREATOR_URL_LIST`

> 说明：`--specified_id` 和 `--creator_id` 命令行覆盖只支持 xhs/bili/dy/wb/ks，贴吧/知乎请直接改配置文件。

### 3) 选择“爬多少、爬多快”

在 `config/base_config.py` 中常用控制项：

- `START_PAGE`：从第几页开始
- `CRAWLER_MAX_NOTES_COUNT`：最多爬多少条帖子/视频
- `MAX_CONCURRENCY_NUM`：并发数量
- `CRAWLER_MAX_SLEEP_SEC`：每次请求间隔

### 4) 评论与媒体开关

- 评论：
  - `ENABLE_GET_COMMENTS`：是否爬一级评论
  - `CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES`：单条评论上限
  - `ENABLE_GET_SUB_COMMENTS`：是否爬二级评论
- 媒体（图/视频）：
  - `ENABLE_GET_MEIDAS`：是否下载媒体文件

### 5) 控制“保存哪些字段”（最关键）

每个平台的“帖子字段映射”都在各自的 `store/<platform>/__init__.py` 中，你需要改这些函数里的 `local_db_item`（或同类字典）：

- 小红书：`store/xhs/__init__.py` → `update_xhs_note`
- 抖音：`store/douyin/__init__.py` → `update_douyin_aweme`
- 快手：`store/kuaishou/__init__.py` → `update_kuaishou_video`
- B站：`store/bilibili/__init__.py` → `update_bilibili_video`
- 微博：`store/weibo/__init__.py` → `update_weibo_note`
- 贴吧：`store/tieba/__init__.py` → `update_tieba_note`
- 知乎：`store/zhihu/__init__.py` → `update_zhihu_content`

评论字段在对应的 `update_*_comment` 函数中。

> 如果你使用 `db/sqlite/postgres` 保存，还需要同步修改表结构/模型和对应的 `_store_impl.py` 写入逻辑；  
> 如果使用 `csv/json/excel`，只需要改字段映射和写入即可。

## 📦 二次开发版本（V3）

本分支 (`feature/video-upload`) 在原版基础上增加了以下功能：
- Web Dashboard 控制面板（密码保护）
- 批量爬取调度器（Excel 作者列表 → 自动逐个爬取）
- 断点续爬 + 优雅中止（不丢数据）
- 飞书多维表格集成（含图片/视频上传）
- 反爬策略可视化配置

**详细部署文档：[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

## WebUI支持

<details>
<summary>🖥️ <strong>WebUI 可视化操作界面</strong></summary>

MediaCrawler 提供了基于 Web 的可视化操作界面，无需命令行也能轻松使用爬虫功能。

#### 启动 WebUI 服务

```shell
# 启动 API 服务器（默认端口 8080）
uv run uvicorn api.main:app --port 8080 --reload

# 或者使用模块方式启动
uv run python -m api.main
```

启动成功后，访问 `http://localhost:8080` 即可打开 WebUI 界面。

#### WebUI 功能特性

- 可视化配置爬虫参数（平台、登录方式、爬取类型等）
- 实时查看爬虫运行状态和日志
- 数据预览和导出

#### 界面预览

<img src="docs/static/images/img_8.png" alt="WebUI 界面预览">

</details>

<details>
<summary>🔗 <strong>使用 Python 原生 venv 管理环境（不推荐）</strong></summary>

#### 创建并激活 Python 虚拟环境

> 如果是爬取抖音和知乎，需要提前安装 nodejs 环境，版本大于等于：`16` 即可

```shell
# 进入项目根目录
cd MediaCrawler

# 创建虚拟环境
# 我的 python 版本是：3.11 requirements.txt 中的库是基于这个版本的
# 如果是其他 python 版本，可能 requirements.txt 中的库不兼容，需自行解决
python -m venv venv

# macOS & Linux 激活虚拟环境
source venv/bin/activate

# Windows 激活虚拟环境
venv\Scripts\activate
```

#### 安装依赖库

```shell
pip install -r requirements.txt
```

#### 安装 playwright 浏览器驱动

```shell
playwright install
```

#### 运行爬虫程序（原生环境）

```shell
# 项目默认是没有开启评论爬取模式，如需评论请在 config/base_config.py 中的 ENABLE_GET_COMMENTS 变量修改
# 一些其他支持项，也可以在 config/base_config.py 查看功能，写的有中文注释

# 从配置文件中读取关键词搜索相关的帖子并爬取帖子信息与评论
python main.py --platform xhs --lt qrcode --type search

# 从配置文件中读取指定的帖子ID列表获取指定帖子的信息与评论信息
python main.py --platform xhs --lt qrcode --type detail

# 打开对应APP扫二维码登录

# 其他平台爬虫使用示例，执行下面的命令查看
python main.py --help
```

</details>


## 💾 数据保存

MediaCrawler 支持多种数据存储方式，包括 CSV、JSON、Excel、SQLite 和 MySQL 数据库。

📖 **详细使用说明请查看：[数据存储指南](docs/data_storage_guide.md)**


## 📚 参考

- **小红书签名仓库**：[Cloxl 的 xhs 签名仓库](https://github.com/Cloxl/xhshow)
- **小红书客户端**：[ReaJason 的 xhs 仓库](https://github.com/ReaJason/xhs)
- **短信转发**：[SmsForwarder 参考仓库](https://github.com/pppscn/SmsForwarder)
- **内网穿透工具**：[ngrok 官方文档](https://ngrok.com/docs/)


# 免责声明
<div id="disclaimer"> 

## 1. 项目目的与性质
本项目（以下简称“本项目”）是作为一个技术研究与学习工具而创建的，旨在探索和学习网络数据采集技术。本项目专注于自媒体平台的数据爬取技术研究，旨在提供给学习者和研究者作为技术交流之用。

## 2. 法律合规性声明
本项目开发者（以下简称“开发者”）郑重提醒用户在下载、安装和使用本项目时，严格遵守中华人民共和国相关法律法规，包括但不限于《中华人民共和国网络安全法》、《中华人民共和国反间谍法》等所有适用的国家法律和政策。用户应自行承担一切因使用本项目而可能引起的法律责任。

## 3. 使用目的限制
本项目严禁用于任何非法目的或非学习、非研究的商业行为。本项目不得用于任何形式的非法侵入他人计算机系统，不得用于任何侵犯他人知识产权或其他合法权益的行为。用户应保证其使用本项目的目的纯属个人学习和技术研究，不得用于任何形式的非法活动。

## 4. 免责声明
开发者已尽最大努力确保本项目的正当性及安全性，但不对用户使用本项目可能引起的任何形式的直接或间接损失承担责任。包括但不限于由于使用本项目而导致的任何数据丢失、设备损坏、法律诉讼等。

## 5. 知识产权声明
本项目的知识产权归开发者所有。本项目受到著作权法和国际著作权条约以及其他知识产权法律和条约的保护。用户在遵守本声明及相关法律法规的前提下，可以下载和使用本项目。

## 6. 最终解释权
关于本项目的最终解释权归开发者所有。开发者保留随时更改或更新本免责声明的权利，恕不另行通知。
</div>

