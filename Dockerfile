# ============================================================
# MediaCrawler Docker Image
# 基于 Python 3.11，内置 Playwright Chromium 浏览器
# 单镜像同时承载 API 和爬虫（爬虫由 API 按需 subprocess 启动）
# ============================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /app

# ---- 系统依赖（含 Playwright Chromium 运行时依赖）----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl fonts-noto-cjk fonts-unifont locales \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2t64 libxshmfence1 \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖（用 tomllib 正确解析 pyproject.toml）----
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
       $(python -c "import tomllib,pathlib;d=tomllib.loads(pathlib.Path('pyproject.toml').read_text());print(' '.join(d['project']['dependencies']))")

# ---- Playwright Chromium（跳过系统依赖安装，上面已手动安装）----
RUN playwright install chromium

# ---- conda shim（让代码中 conda run -n uvenv ... 在容器内透传执行）----
RUN printf '#!/bin/bash\nshift\nwhile [[ $# -gt 0 ]]; do\n  case "$1" in\n    --*) shift ;;\n    -n)  shift; shift ;;\n    *)   break ;;\n  esac\ndone\nexec "$@"\n' \
    > /usr/local/bin/conda && chmod +x /usr/local/bin/conda

# ---- 复制项目代码 ----
COPY . .

# ---- 中文字体（词云图用） ----
RUN if [ -f docs/STZHONGS.TTF ]; then \
      mkdir -p /usr/share/fonts/custom && \
      cp docs/STZHONGS.TTF /usr/share/fonts/custom/ && \
      fc-cache -f; \
    fi

# ---- 创建数据目录 ----
RUN mkdir -p data/cookies data/xhs browser_data logs

EXPOSE 9001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -sf http://localhost:9001/api/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "9001"]
