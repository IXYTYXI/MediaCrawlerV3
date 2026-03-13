# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/cmd_arg/arg.py
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


from __future__ import annotations


import sys
from enum import Enum
from types import SimpleNamespace
from typing import Iterable, Optional, Sequence, Type, TypeVar

import typer
from typing_extensions import Annotated

import config
from tools.utils import str2bool


EnumT = TypeVar("EnumT", bound=Enum)


class PlatformEnum(str, Enum):
    """Supported media platform enumeration"""

    XHS = "xhs"
    DOUYIN = "dy"
    KUAISHOU = "ks"
    BILIBILI = "bili"
    WEIBO = "wb"
    TIEBA = "tieba"
    ZHIHU = "zhihu"


class LoginTypeEnum(str, Enum):
    """Login type enumeration"""

    QRCODE = "qrcode"
    PHONE = "phone"
    COOKIE = "cookie"


class CrawlerTypeEnum(str, Enum):
    """Crawler type enumeration"""

    SEARCH = "search"
    DETAIL = "detail"
    CREATOR = "creator"
    SEARCH_TOP = "search_top"
    CREATOR_KEYWORD = "creator_keyword"


class SaveDataOptionEnum(str, Enum):
    """Data save option enumeration"""

    CSV = "csv"
    DB = "db"
    JSON = "json"
    SQLITE = "sqlite"
    MONGODB = "mongodb"
    EXCEL = "excel"
    POSTGRES = "postgres"


class InitDbOptionEnum(str, Enum):
    """Database initialization option"""

    SQLITE = "sqlite"
    MYSQL = "mysql"
    POSTGRES = "postgres"


def _to_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str2bool(value)


def _coerce_enum(
    enum_cls: Type[EnumT],
    value: EnumT | str,
    default: EnumT,
) -> EnumT:
    """Safely convert a raw config value to an enum member."""

    if isinstance(value, enum_cls):
        return value

    try:
        return enum_cls(value)
    except ValueError:
        typer.secho(
            f"⚠️ Config value '{value}' is not within the supported range of {enum_cls.__name__}, falling back to default value '{default.value}'.",
            fg=typer.colors.YELLOW,
        )
        return default


def _normalize_argv(argv: Optional[Sequence[str]]) -> Iterable[str]:
    if argv is None:
        return list(sys.argv[1:])
    return list(argv)


def _inject_init_db_default(args: Sequence[str]) -> list[str]:
    """Ensure bare --init_db defaults to sqlite for backward compatibility."""

    normalized: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        normalized.append(arg)

        if arg == "--init_db":
            next_arg = args[i + 1] if i + 1 < len(args) else None
            if not next_arg or next_arg.startswith("-"):
                normalized.append(InitDbOptionEnum.SQLITE.value)
        i += 1

    return normalized


async def parse_cmd(argv: Optional[Sequence[str]] = None):
    """Parse command line arguments using Typer."""

    app = typer.Typer(add_completion=False)

    @app.callback(invoke_without_command=True)
    def main(
        platform: Annotated[
            PlatformEnum,
            typer.Option(
                "--platform",
                help="Media platform selection (xhs=XiaoHongShu | dy=Douyin | ks=Kuaishou | bili=Bilibili | wb=Weibo | tieba=Baidu Tieba | zhihu=Zhihu)",
                rich_help_panel="Basic Configuration",
            ),
        ] = _coerce_enum(PlatformEnum, config.PLATFORM, PlatformEnum.XHS),
        lt: Annotated[
            LoginTypeEnum,
            typer.Option(
                "--lt",
                help="Login type (qrcode=QR Code | phone=Phone | cookie=Cookie)",
                rich_help_panel="Account Configuration",
            ),
        ] = _coerce_enum(LoginTypeEnum, config.LOGIN_TYPE, LoginTypeEnum.QRCODE),
        crawler_type: Annotated[
            CrawlerTypeEnum,
            typer.Option(
                "--type",
                help="Crawler type (search=Search | detail=Detail | creator=Creator | creator_keyword=Creator+Keyword)",
                rich_help_panel="Basic Configuration",
            ),
        ] = _coerce_enum(CrawlerTypeEnum, config.CRAWLER_TYPE, CrawlerTypeEnum.SEARCH),
        start: Annotated[
            int,
            typer.Option(
                "--start",
                help="Starting page number",
                rich_help_panel="Basic Configuration",
            ),
        ] = config.START_PAGE,
        keywords: Annotated[
            str,
            typer.Option(
                "--keywords",
                help="Enter keywords, multiple keywords separated by commas",
                rich_help_panel="Basic Configuration",
            ),
        ] = config.KEYWORDS,
        keywords_combine: Annotated[
            bool,
            typer.Option(
                "--keywords-combine/--no-keywords-combine",
                help="Combine multiple keywords as one search (A,B,C → 'A B C')",
                rich_help_panel="Basic Configuration",
            ),
        ] = getattr(config, "KEYWORDS_COMBINE_MODE", False),
        get_detail: Annotated[
            str,
            typer.Option(
                "--get_detail",
                help="Whether to get note details (likes, collects, etc.), supports yes/true/t/y/1 or no/false/f/n/0",
                rich_help_panel="Content Configuration",
                show_default=True,
            ),
        ] = str(config.ENABLE_GET_NOTE_DETAIL),
        get_comment: Annotated[
            str,
            typer.Option(
                "--get_comment",
                help="Whether to crawl first-level comments, supports yes/true/t/y/1 or no/false/f/n/0",
                rich_help_panel="Comment Configuration",
                show_default=True,
            ),
        ] = str(config.ENABLE_GET_COMMENTS),
        get_sub_comment: Annotated[
            str,
            typer.Option(
                "--get_sub_comment",
                help="Whether to crawl second-level comments, supports yes/true/t/y/1 or no/false/f/n/0",
                rich_help_panel="Comment Configuration",
                show_default=True,
            ),
        ] = str(config.ENABLE_GET_SUB_COMMENTS),
        headless: Annotated[
            str,
            typer.Option(
                "--headless",
                help="Whether to enable headless mode (applies to both Playwright and CDP), supports yes/true/t/y/1 or no/false/f/n/0",
                rich_help_panel="Runtime Configuration",
                show_default=True,
            ),
        ] = str(config.HEADLESS),
        save_data_option: Annotated[
            SaveDataOptionEnum,
            typer.Option(
                "--save_data_option",
                help="Data save option (csv=CSV file | db=MySQL database | json=JSON file | sqlite=SQLite database | mongodb=MongoDB database | excel=Excel file | postgres=PostgreSQL database)",
                rich_help_panel="Storage Configuration",
            ),
        ] = _coerce_enum(
            SaveDataOptionEnum, config.SAVE_DATA_OPTION, SaveDataOptionEnum.JSON
        ),
        init_db: Annotated[
            Optional[InitDbOptionEnum],
            typer.Option(
                "--init_db",
                help="Initialize database table structure (sqlite | mysql | postgres)",
                rich_help_panel="Storage Configuration",
            ),
        ] = None,
        cookies: Annotated[
            str,
            typer.Option(
                "--cookies",
                help="Cookie value used for Cookie login method",
                rich_help_panel="Account Configuration",
            ),
        ] = config.COOKIES,
        specified_id: Annotated[
            str,
            typer.Option(
                "--specified_id",
                help="Post/video ID list in detail mode, multiple IDs separated by commas (supports full URL or ID)",
                rich_help_panel="Basic Configuration",
            ),
        ] = "",
        creator_id: Annotated[
            str,
            typer.Option(
                "--creator_id",
                help="Creator ID list in creator mode, multiple IDs separated by commas (supports full URL or ID)",
                rich_help_panel="Basic Configuration",
            ),
        ] = "",
        max_comments_count_singlenotes: Annotated[
            int,
            typer.Option(
                "--max_comments_count_singlenotes",
                help="Maximum number of first-level comments to crawl per post/video",
                rich_help_panel="Comment Configuration",
            ),
        ] = config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
        max_sub_comments_per_comment: Annotated[
            int,
            typer.Option(
                "--max_sub_comments_per_comment",
                help="Maximum number of sub-comments to crawl per first-level comment (0 = unlimited)",
                rich_help_panel="Comment Configuration",
            ),
        ] = config.CRAWLER_MAX_SUB_COMMENTS_PER_COMMENT,
        top_notes_count: Annotated[
            int,
            typer.Option(
                "--top_notes_count",
                help="[search_top] Number of top-liked notes to keep",
                rich_help_panel="Search Top Configuration",
            ),
        ] = getattr(config, "SEARCH_TOP_NOTES_COUNT", 100),
        top_comment_notes_count: Annotated[
            int,
            typer.Option(
                "--top_comment_notes_count",
                help="[search_top] Number of top-liked notes to crawl comments for",
                rich_help_panel="Search Top Configuration",
            ),
        ] = getattr(config, "SEARCH_TOP_COMMENT_NOTES_COUNT", 20),
        comment_page_count: Annotated[
            int,
            typer.Option(
                "--comment_page_count",
                help="[search_top] Number of comment pages to crawl per note",
                rich_help_panel="Search Top Configuration",
            ),
        ] = getattr(config, "SEARCH_TOP_COMMENT_PAGE_COUNT", 2),
        feishu_folder: Annotated[
            str,
            typer.Option(
                "--feishu_folder",
                help="飞书文件夹 token，覆盖配置文件中的默认值（为空则使用配置文件）",
                rich_help_panel="Feishu Configuration",
            ),
        ] = "",
        filter_keywords: Annotated[
            str,
            typer.Option(
                "--filter_keywords",
                help="[creator_keyword] Filter keywords (| = OR, & = AND, space = compound), comma-separated for multiple",
                rich_help_panel="Creator+Keyword Configuration",
            ),
        ] = getattr(config, "CREATOR_KEYWORD_FILTER_KEYWORDS", ""),
        filter_scope: Annotated[
            str,
            typer.Option(
                "--filter_scope",
                help="[creator_keyword] Filter scope, comma-separated (title,desc,tags,comments,author_desc)",
                rich_help_panel="Creator+Keyword Configuration",
            ),
        ] = "title,desc,tags",
        sort_type: Annotated[
            str,
            typer.Option(
                "--sort_type",
                help="Search sort type (general=综合 | popularity_descending=热度 | time_descending=最新)",
                rich_help_panel="Basic Configuration",
            ),
        ] = getattr(config, "SORT_TYPE", "general"),
        max_notes: Annotated[
            int,
            typer.Option(
                "--max_notes",
                help="Maximum number of notes to crawl (overrides CRAWLER_MAX_NOTES_COUNT)",
                rich_help_panel="Basic Configuration",
            ),
        ] = config.CRAWLER_MAX_NOTES_COUNT,
        date_start: Annotated[
            str,
            typer.Option(
                "--date_start",
                help="Filter: only keep notes published on or after this date (YYYY-MM-DD)",
                rich_help_panel="Date Filter",
            ),
        ] = getattr(config, "CRAWL_DATE_START", ""),
        date_end: Annotated[
            str,
            typer.Option(
                "--date_end",
                help="Filter: only keep notes published on or before this date (YYYY-MM-DD)",
                rich_help_panel="Date Filter",
            ),
        ] = getattr(config, "CRAWL_DATE_END", ""),
        transfer_owner: Annotated[
            str,
            typer.Option(
                "--transfer_owner",
                help="是否转移多维表格所有者（true/false/空=跟随全局）",
                rich_help_panel="Feishu Configuration",
            ),
        ] = "",
        owner_open_id: Annotated[
            str,
            typer.Option(
                "--owner_open_id",
                help="转移所有者的 Open ID（留空则用全局配置）",
                rich_help_panel="Feishu Configuration",
            ),
        ] = "",
        collaborator_open_id: Annotated[
            Optional[list[str]],
            typer.Option(
                "--collaborator_open_id",
                help="协作者 Open ID（可多次指定，留空则用全局配置）",
                rich_help_panel="Feishu Configuration",
            ),
        ] = None,
        collaborator_user_id: Annotated[
            Optional[list[str]],
            typer.Option(
                "--collaborator_user_id",
                help="协作者 User ID（可多次指定，留空则用全局配置）",
                rich_help_panel="Feishu Configuration",
            ),
        ] = None,
        notification_chat_id: Annotated[
            str,
            typer.Option(
                "--notification_chat_id",
                help="飞书通知群聊 ID（任务完成后发消息到此群，留空则用全局配置）",
                rich_help_panel="Notification",
            ),
        ] = getattr(config, "NOTIFICATION_CHAT_ID_OVERRIDE", ""),
        notification_webhook_url: Annotated[
            str,
            typer.Option(
                "--notification_webhook_url",
                help="飞书通知 Webhook URL（任务完成后发消息，留空则用全局配置）",
                rich_help_panel="Notification",
            ),
        ] = getattr(config, "NOTIFICATION_WEBHOOK_URL_OVERRIDE", ""),
    ) -> SimpleNamespace:
        """MediaCrawler 命令行入口"""

        enable_detail = _to_bool(get_detail)
        enable_comment = _to_bool(get_comment)
        enable_sub_comment = _to_bool(get_sub_comment)
        enable_headless = _to_bool(headless)
        init_db_value = init_db.value if init_db else None

        # Parse specified_id and creator_id into lists
        specified_id_list = [id.strip() for id in specified_id.split(",") if id.strip()] if specified_id else []
        creator_id_list = [id.strip() for id in creator_id.split(",") if id.strip()] if creator_id else []

        # override global config
        config.PLATFORM = platform.value
        config.LOGIN_TYPE = lt.value
        config.CRAWLER_TYPE = crawler_type.value
        config.START_PAGE = start
        config.KEYWORDS = keywords
        config.KEYWORDS_COMBINE_MODE = keywords_combine
        config.ENABLE_GET_NOTE_DETAIL = enable_detail
        config.ENABLE_GET_COMMENTS = enable_comment
        config.ENABLE_GET_SUB_COMMENTS = enable_sub_comment
        config.HEADLESS = enable_headless
        config.CDP_HEADLESS = enable_headless
        config.SAVE_DATA_OPTION = save_data_option.value
        config.COOKIES = cookies
        config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = max_comments_count_singlenotes
        config.CRAWLER_MAX_SUB_COMMENTS_PER_COMMENT = max_sub_comments_per_comment
        config.SEARCH_TOP_NOTES_COUNT = top_notes_count
        config.SEARCH_TOP_COMMENT_NOTES_COUNT = top_comment_notes_count
        config.SEARCH_TOP_COMMENT_PAGE_COUNT = comment_page_count
        if feishu_folder:
            config.FEISHU_FOLDER_TOKEN_OVERRIDE = feishu_folder

        config.CREATOR_KEYWORD_FILTER_KEYWORDS = filter_keywords
        config.CREATOR_KEYWORD_FILTER_SCOPE = filter_scope
        config.SORT_TYPE = sort_type
        config.CRAWLER_MAX_NOTES_COUNT = max_notes
        config.CRAWL_DATE_START = date_start
        config.CRAWL_DATE_END = date_end
        if transfer_owner.lower() in ("true", "1", "yes"):
            setattr(config, "TRANSFER_OWNER_OVERRIDE", True)
        elif transfer_owner.lower() in ("false", "0", "no"):
            setattr(config, "TRANSFER_OWNER_OVERRIDE", False)
        if owner_open_id:
            setattr(config, "OWNER_OPEN_ID_OVERRIDE", owner_open_id)
        if collaborator_open_id:
            setattr(config, "COLLABORATOR_OPEN_IDS_OVERRIDE", [x for x in collaborator_open_id if x.strip()])
        if collaborator_user_id:
            setattr(config, "COLLABORATOR_USER_IDS_OVERRIDE", [x for x in collaborator_user_id if x.strip()])
        setattr(config, "NOTIFICATION_CHAT_ID_OVERRIDE", notification_chat_id or "")
        setattr(config, "NOTIFICATION_WEBHOOK_URL_OVERRIDE", notification_webhook_url or "")

        # Set platform-specific ID lists for detail/creator mode
        if specified_id_list:
            if platform == PlatformEnum.XHS:
                config.XHS_SPECIFIED_NOTE_URL_LIST = specified_id_list
            elif platform == PlatformEnum.BILIBILI:
                config.BILI_SPECIFIED_ID_LIST = specified_id_list
            elif platform == PlatformEnum.DOUYIN:
                config.DY_SPECIFIED_ID_LIST = specified_id_list
            elif platform == PlatformEnum.WEIBO:
                config.WEIBO_SPECIFIED_ID_LIST = specified_id_list
            elif platform == PlatformEnum.KUAISHOU:
                config.KS_SPECIFIED_ID_LIST = specified_id_list

        if creator_id_list:
            if platform == PlatformEnum.XHS:
                config.XHS_CREATOR_ID_LIST = creator_id_list
            elif platform == PlatformEnum.BILIBILI:
                config.BILI_CREATOR_ID_LIST = creator_id_list
            elif platform == PlatformEnum.DOUYIN:
                config.DY_CREATOR_ID_LIST = creator_id_list
            elif platform == PlatformEnum.WEIBO:
                config.WEIBO_CREATOR_ID_LIST = creator_id_list
            elif platform == PlatformEnum.KUAISHOU:
                config.KS_CREATOR_ID_LIST = creator_id_list

        return SimpleNamespace(
            platform=config.PLATFORM,
            lt=config.LOGIN_TYPE,
            type=config.CRAWLER_TYPE,
            start=config.START_PAGE,
            keywords=config.KEYWORDS,
            get_comment=config.ENABLE_GET_COMMENTS,
            get_sub_comment=config.ENABLE_GET_SUB_COMMENTS,
            headless=config.HEADLESS,
            save_data_option=config.SAVE_DATA_OPTION,
            init_db=init_db_value,
            cookies=config.COOKIES,
            specified_id=specified_id,
            creator_id=creator_id,
        )

    command = typer.main.get_command(app)

    cli_args = _normalize_argv(argv)
    cli_args = _inject_init_db_default(cli_args)

    try:
        result = command.main(args=cli_args, standalone_mode=False)
        if isinstance(result, int):  # help/options handled by Typer; propagate exit code
            raise SystemExit(result)
        return result
    except typer.Exit as exc:  # pragma: no cover - CLI exit paths
        raise SystemExit(exc.exit_code) from exc
