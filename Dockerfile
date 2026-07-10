# ── 构建阶段 ──
FROM python:3.12-slim AS builder

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── 运行阶段 ──
FROM python:3.12-slim

WORKDIR /app

# 从构建阶段复制已安装的包
COPY --from=builder /root/.local /root/.local

# 复制源码
COPY . .

# 创建必要目录
RUN mkdir -p /tmp/book_digest_uploads /workspace/test_books

# 环境变量默认值
ENV BD_API_KEY=""
ENV BD_BASE_URL="https://api.siliconflow.cn/v1"
ENV BD_MODEL="Qwen/Qwen3.5-9B"
ENV BD_UPLOAD_DIR="/tmp/book_digest_uploads"
ENV BD_MAX_UPLOAD_SIZE="52428800"
ENV BD_JOB_TIMEOUT="1800"
ENV BD_CORS_ORIGINS="*"
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "uvicorn", "book_digest.server:app", "--host", "0.0.0.0", "--port", "8000"]
