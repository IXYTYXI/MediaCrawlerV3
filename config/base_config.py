# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# 基础配置
import platform as _platform
_IS_LINUX = _platform.system() == "Linux"

PLATFORM = "xhs"  # 平台，xhs | dy | ks | bili | wb | tieba | zhihu
KEYWORDS = "编程副业,编程兼职"  # 关键词搜索配置，以英文逗号分隔
LOGIN_TYPE = "cookie"  # qrcode or phone or cookie
COOKIES = "web_session=040069b8083b3cc3b8d61fe4aa3b4b31c37ce7"

# ==================== 手动 Session 回退 ====================
# 当扫码登录失败、shared cookie 过期、浏览器 session 过期时，
# 程序会检查此值作为最后的回退手段。
# 使用方法：在浏览器中登录小红书，F12 → Application → Cookies → 复制 web_session 的值粘贴到这里。
# 也可以直接编辑 data/cookies/manual_web_session.txt（一行纯文本，仅放 web_session 值）。
MANUAL_WEB_SESSION = ""
CRAWLER_TYPE = (
    "creator"  # 爬取类型，search(关键词搜索) | detail(帖子详情)| creator(创作者主页数据)
)
# 是否开启 IP 代理
ENABLE_IP_PROXY = False

# 代理IP池数量
IP_PROXY_POOL_COUNT = 2

# 代理IP提供商名称
IP_PROXY_PROVIDER_NAME = "kuaidaili"  # kuaidaili | wandouhttp

# 设置为True不会打开浏览器（无头浏览器）
# 设置False会打开一个浏览器
# 小红书如果一直扫码登录不通过，打开浏览器手动过一下滑动验证码
# 抖音如果一直提示失败，打开浏览器看下是否扫码登录之后出现了手机号验证，如果出现了手动过一下再试。
# Linux 服务器无图形界面，自动设为 True
HEADLESS = True if _IS_LINUX else False

# 是否保存登录状态
SAVE_LOGIN_STATE = True

# ==================== CDP (Chrome DevTools Protocol) 配置 ====================
# 是否启用CDP模式 - 使用用户现有的Chrome/Edge浏览器进行爬取，提供更好的反检测能力
# 启用后将自动检测并启动用户的Chrome/Edge浏览器，通过CDP协议进行控制
# 这种方式使用真实的浏览器环境，包括用户的扩展、Cookie和设置，大大降低被检测的风险
# Linux 服务器无 Chrome Dev，自动关闭 CDP，使用 Playwright 内置 Chromium
ENABLE_CDP_MODE = False if _IS_LINUX else True

# CDP调试端口，用于与浏览器通信
# 如果端口被占用，系统会自动尝试下一个可用端口
CDP_DEBUG_PORT = 9222

# 自定义浏览器路径（可选）
# 如果为空，系统会自动检测Chrome/Edge的安装路径
# Windows示例: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
# macOS示例: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CUSTOM_BROWSER_PATH = ""

# CDP模式下是否启用无头模式
# 注意：即使设置为True，某些反检测功能在无头模式下可能效果不佳
CDP_HEADLESS = True if _IS_LINUX else False

# 浏览器启动超时时间（秒）
BROWSER_LAUNCH_TIMEOUT = 120

# 是否在程序结束时自动关闭浏览器
# 设置为False可以保持浏览器运行，便于调试
AUTO_CLOSE_BROWSER = False

# 数据保存类型选项配置,支持六种类型：csv、db、json、sqlite、excel、postgres, 最好保存到DB，有排重的功能。
SAVE_DATA_OPTION = "json"  # csv or db or json or sqlite or excel or postgres

# 用户浏览器缓存的浏览器文件配置
USER_DATA_DIR = "%s_user_data_dir"  # %s will be replaced by platform name

# 爬取开始页数 默认从第一页开始
START_PAGE = 1

# 爬取视频/帖子的数量控制（总数量上限）
CRAWLER_MAX_NOTES_COUNT = 3000

# 并发爬虫数量控制（建议 2-3，太高容易触发反爬）
MAX_CONCURRENCY_NUM = 3

# 是否开启爬媒体模式（包含图片或视频资源），默认不开启爬媒体
ENABLE_GET_MEIDAS = True

# 是否获取作品详情（点赞数、收藏数等），关闭则只获取基本信息
ENABLE_GET_NOTE_DETAIL = True

# 是否开启爬评论模式, 默认开启爬评论
ENABLE_GET_COMMENTS = False

# 爬取一级评论的数量控制(单视频/帖子)
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 8000

# 评论获取模式: "parallel" = 边爬作品边爬评论, "sequential" = 先爬完作品再爬评论
COMMENTS_FETCH_MODE = "parallel"

# 并行模式下，获取作品详情后延迟多少秒再获取评论
COMMENTS_DELAY_SEC = 1.0

# 评论获取的并发数（建议1-3）
COMMENTS_CONCURRENCY = 2

# 是否开启爬二级评论模式, 默认不开启爬二级评论
# 老版本项目使用了 db, 则需参考 schema/tables.sql line 287 增加表字段
ENABLE_GET_SUB_COMMENTS = False

# 词云相关
# 是否开启生成评论词云图
ENABLE_GET_WORDCLOUD = False
# 自定义词语及其分组
# 添加规则：xx:yy 其中xx为自定义添加的词组，yy为将xx该词组分到的组名。
CUSTOM_WORDS = {
    "零几": "年份",  # 将“零几”识别为一个整体
    "高频词": "专业术语",  # 示例自定义词
}

# 停用(禁用)词文件路径
STOP_WORDS_FILE = "./docs/hit_stopwords.txt"

# 中文字体文件路径
FONT_PATH = "./docs/STZHONGS.TTF"

# 爬取间隔时间（固定等待）
CRAWLER_MAX_SLEEP_SEC = 2

# 随机等待（反反爬策略）- 保守模式
RANDOM_SLEEP_ENABLED = True
RANDOM_SLEEP_MIN_SEC = 5.0
RANDOM_SLEEP_MAX_SEC = 10.0

# 评论抓取的随机等待（单独控制）
RANDOM_SLEEP_COMMENTS_MIN_SEC = 3.0
RANDOM_SLEEP_COMMENTS_MAX_SEC = 6.0

# 批次暂停策略：每爬取 N 条后，额外暂停一段时间
BATCH_PAUSE_ENABLED = True
BATCH_PAUSE_EVERY_N = 10  # 每 10 条
BATCH_PAUSE_MIN_SEC = 30.0  # 暂停 30~60 秒
BATCH_PAUSE_MAX_SEC = 60.0

# 老作品只更新互动数据：已爬取过的作品仅获取最新点赞/评论数，不重新下载图片视频
# 由 batch_crawl.stats_update_for_crawled 配置
ENABLE_STATS_UPDATE_FOR_CRAWLED = False

# 由 batch_crawler 运行时注入，供爬虫「老作品只更新互动数据」时加载已有数据
XHS_TASK_DIR = ""
XHS_CURRENT_USER_ID = ""

# 按日期提前停止：当连续 N 条作品早于 date_start 时，停止爬取该作者剩余作品
# 由 batch_crawler 在运行时动态设置 DATE_EARLY_STOP_THRESHOLD 和 CRAWL_DATE_START
DATE_EARLY_STOP_ENABLED = False
DATE_EARLY_STOP_THRESHOLD = 5  # 连续多少条超出范围后停止
CRAWL_DATE_START = ""  # 由 batch_crawler 动态注入，格式 "2025-01-01"

# 假动作策略：随机访问无关内容，模拟真实用户浏览
FAKE_ACTION_ENABLED = True
FAKE_ACTION_PROBABILITY = 0.15  # 15% 概率触发假动作
FAKE_ACTION_MIN_SEC = 2.0  # 假动作后等待 2~5 秒
FAKE_ACTION_MAX_SEC = 5.0

from .bilibili_config import *
from .xhs_config import *
from .dy_config import *
from .ks_config import *
from .weibo_config import *
from .tieba_config import *
from .zhihu_config import *
