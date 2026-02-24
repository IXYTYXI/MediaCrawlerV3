# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/utils.py
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


import argparse
import logging
import os
from logging.handlers import TimedRotatingFileHandler

from .crawler_util import *
from .slider_util import *
from .time_util import *


def init_loging_config():
    level = logging.INFO
    log_format = "%(asctime)s %(name)s %(levelname)s (%(filename)s:%(lineno)d) - %(message)s"
    date_format = '%Y-%m-%d %H:%M:%S'

    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
    )
    _logger = logging.getLogger("MediaCrawler")
    _logger.setLevel(level)

    # 日志文件：按天轮转，保留 14 天，第 15 天自动删除
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "crawler.log")

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",       # 每天零点轮转
        interval=1,
        backupCount=14,        # 保留 14 个旧文件（即 14 天，第 15 天删除最早的）
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"  # 旧文件名格式: crawler.log.2026-02-13
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    _logger.addHandler(file_handler)

    # Disable httpx INFO level logs
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return _logger


logger = init_loging_config()

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')
