#!/bin/bash
set -e
cd "$(dirname "$0")"

VENV_DIR="venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "创建 Python 虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

if ! python -c "import flask" 2>/dev/null; then
    echo "安装依赖..."
    pip install -r requirements.txt -q
fi

echo "启动 OpenClaw LLM 日志查看器..."
echo "访问地址: http://127.0.0.1:5001"
python app.py
