#!/bin/bash
unset PLAYWRIGHT_BROWSERS_PATH
export PYTHONUNBUFFERED=1
cd /data/vonjan/program/MediaCrawler-stable
exec conda run --no-capture-output -n uvenv python -u -m uvicorn api.main:app --host 0.0.0.0 --port 9001
