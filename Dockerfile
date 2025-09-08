# 轻量且稳定
FROM python:3.11-slim

# 让 python 输出立即刷新（日志更友好）
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ---- 安装系统依赖：ffmpeg + Playwright 运行所需组件 ----
# 注意：playwright 的 "--with-deps" 仍需基础包，这里一次性装齐
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libdrm2 \
    libdbus-1-3 \
    libgbm1 \
    libgtk-3-0 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    wget \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖清单，利用 Docker 缓存
COPY requirements.txt /app/requirements.txt

# 安装 Python 依赖
RUN pip install -r requirements.txt

# 安装 Playwright 及 Chromium（含额外依赖）
RUN python -m playwright install --with-deps chromium

# 复制项目代码
COPY . /app

# Render 会注入 PORT 环境变量，这里用它，默认 10000
ENV PORT=10000

# 使用 gunicorn 启动 Flask：app.py 内必须有 app = Flask(...)
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-10000} app:app"]
