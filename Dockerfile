# ============================================================
# MediaCrawler Docker Image
# 基于 Python 3.11，内置 Playwright Chromium 浏览器
# ============================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /app

# ---- 系统依赖 ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    fonts-noto-cjk \
    locales \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ----
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
       $(python -c "
import ast, pathlib
data = pathlib.Path('pyproject.toml').read_text()
in_deps = False
deps = []
for line in data.splitlines():
    if line.strip().startswith('dependencies'):
        in_deps = True
        continue
    if in_deps:
        if line.strip() == ']':
            break
        dep = line.strip().strip(',').strip('\"').strip(\"'\")
        if dep:
            deps.append(dep)
print(' '.join(deps))
")

# ---- Playwright Chromium ----
RUN playwright install chromium && playwright install-deps

# ---- conda shim（让现有 conda run 命令在容器内正常执行） ----
RUN printf '#!/bin/bash\nshift  # consume "run"\nwhile [[ "$1" == --* ]] || [[ "$1" == -n ]] || [[ "$1" == uvenv ]]; do\n  [[ "$1" == -n ]] && shift  # skip -n and its arg\n  shift\ndone\nexec "$@"\n' > /usr/local/bin/conda \
    && chmod +x /usr/local/bin/conda

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

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "9001"]
