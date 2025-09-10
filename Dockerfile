# 使用 Playwright 官方镜像，内置 Chromium 浏览器与依赖，确保自动化可用
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 工作目录
WORKDIR /app

# 环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright 浏览器（确保 Chrome 可用）
RUN playwright install chrome
RUN playwright install-deps chrome

# 复制应用代码（包含自动化与配置）
COPY main.py .
COPY config.py .
COPY mail_service.py .
COPY automation/ ./automation/
COPY static/ ./static/
COPY config/ ./config/
COPY docker-entrypoint.sh .

# 权限与目录
RUN chmod +x docker-entrypoint.sh \
 && mkdir -p /app/data /app/run-logs

# 端口与健康检查
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/api')" || exit 1

# 启动
CMD ["python", "main.py"]
