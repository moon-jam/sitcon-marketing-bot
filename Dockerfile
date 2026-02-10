# syntax=docker/dockerfile:1

FROM python:3.12-slim

# 安裝 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 設定工作目錄
WORKDIR /app

# 複製依賴檔案
COPY pyproject.toml uv.lock ./

# 安裝依賴（使用 uv）
RUN uv sync --frozen --no-dev --no-install-project

# 複製程式碼
COPY main.py database.py scheduler.py ./
COPY handlers/ ./handlers/

# 建立資料目錄（用於 SQLite）
RUN mkdir -p /app/data

# 設定環境變數
ENV PYTHONUNBUFFERED=1

# 執行
CMD ["uv", "run", "python", "main.py"]
