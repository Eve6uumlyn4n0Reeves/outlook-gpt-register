#!/bin/bash

# 设置默认值
HOST=${HOST:-"0.0.0.0"}
PORT=${PORT:-8000}
WORKERS=${WORKERS:-1}

# 创建必要的目录
mkdir -p /app/data

# 如果accounts.json不存在，创建空的
if [ ! -f "/app/accounts.json" ]; then
    echo "{}" > /app/accounts.json
fi

echo "启动Outlook邮件API服务..."
echo "配置信息:"
echo "   - 主机地址: $HOST"
echo "   - 端口: $PORT"
echo "   - 工作进程: $WORKERS"
echo "   - 数据目录: /app/data"

# 启动应用
exec python main.py