# OpenClaw LLM API 日志查看器

本文档说明如何在 Linux / macOS 上从源码启动 OpenClaw，以及如何使用配套的 LLM API 日志查看工具。

OpenClaw **不需要编译为平台二进制文件**。构建步骤仅是将 TypeScript 编译为 JavaScript（输出到 `dist/`），之后所有功能均通过 `node openclaw.mjs` 直接运行，无需额外安装步骤。

---

## 一、环境依赖检查

### 必需工具

| 工具    | 最低版本 | 用途                           |
| ------- | -------- | ------------------------------ |
| Node.js | 22+      | 运行 OpenClaw（全程使用 node） |
| npm     | 10+      | 随 Node.js 附带                |
| pnpm    | 10+      | 项目包管理器                   |
| Bun     | 1.x      | 构建阶段 TypeScript 脚本执行   |
| Python  | 3.10+    | 日志查看器（Flask，可选）      |

### 逐项检查

```bash
# Node.js（需 22+）
node --version

# npm（需 10+）
npm --version

# pnpm（需 10+）
pnpm --version
# 若未安装：
npm install -g pnpm

# Bun（构建阶段需要）
bun --version
# 若未安装（Linux/macOS）：
curl -fsSL https://bun.sh/install | bash
source ~/.bashrc  # 或 source ~/.zshrc

# Python 3（仅日志查看器需要）
python3 --version
# macOS：brew install python3
# Ubuntu/Debian：sudo apt install python3 python3-venv python3-pip
```

### 安装 Node.js 22（推荐用 nvm）

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash
source ~/.bashrc

nvm install 22
nvm use 22
```

---

## 二、从源码启动 OpenClaw

整体流程：**安装依赖 → Onboard 配置 LLM → 启动 Gateway**。

项目使用 `scripts/run-node.mjs` 作为统一启动入口。该脚本会自动检测 `dist/` 是否过期（对比源码文件时间和 git HEAD），**仅在需要时自动增量编译**，然后直接运行，无需手动执行编译命令。

### 第一步：克隆并安装依赖

```bash
git clone https://github.com/openclaw/openclaw.git
cd openclaw
pnpm install
```

### 第二步：Onboard — 配置 LLM 和渠道

首次使用必须运行 onboard，配置 LLM 提供商（API Key）和消息渠道。配置结果持久化到 `~/.openclaw/`。

```bash
node scripts/run-node.mjs onboard
```

首次运行时脚本会先自动编译 TypeScript，随后进入交互引导：

1. 选择 LLM 提供商（Anthropic、OpenAI 等）并填入 API Key
2. 配置消息渠道（Telegram、Discord、Signal 等）
3. 完成后配置写入 `~/.openclaw/openclaw.json`

> Onboard 只需运行一次。后续修改配置可编辑 `~/.openclaw/openclaw.json` 或重新运行 onboard。

### 第三步：启动 Gateway

Gateway 是 OpenClaw 的核心进程，负责消息路由、LLM 调用、渠道连接等全部功能。**整个应用运行在单个 Node.js 进程中**，没有额外守护进程。

```bash
node scripts/run-node.mjs gateway
```

进程保持前台运行，Ctrl+C 退出。源码有改动时脚本会在启动前自动重新编译。

> `openclaw dashboard` 只是在浏览器中打开 Gateway 内置 Web UI 的 URL，不是独立进程。

---

## 三、LLM API 日志机制

### 日志是什么

OpenClaw 内置了 LLM API 调用日志，记录每次 Agent Run 中所有发送给 LLM 的请求和收到的响应，以便事后审查、调试和性能分析。

### 日志存储位置

```
~/.openclaw/logs/llm-api/<sessionId>.ndjson
```

- 每个 OpenClaw 会话对应一个 `.ndjson` 文件，文件名为 `sessionId`。
- 格式为 **NDJSON**（Newline-Delimited JSON），每行一个 JSON 对象。
- 默认**启用**；如需禁用，设置环境变量：

```bash
export OPENCLAW_LLM_API_LOG=0
```

- 如需自定义日志目录：

```bash
export OPENCLAW_LLM_API_LOG_DIR=/your/custom/path
```

### 日志条目格式

每条日志有三种 `stage`（阶段），对应 Agent Run 的不同时刻：

#### `stage: "request"` — LLM 请求

每次向 LLM API 发送请求时写入，包含完整的请求 payload。

```jsonc
{
  "ts": "2026-03-06T10:00:00.000Z",
  "stage": "request",
  "runId": "...",
  "sessionId": "...",
  "provider": "anthropic",
  "modelId": "claude-sonnet-4-6",
  "modelApi": "anthropic-messages",
  "workspaceDir": "/home/user/project",
  "callIndex": 1,          // 本 run 内第几次 LLM 调用（从 1 起）
  "payload": { ... },      // 发送给 LLM API 的完整 JSON 请求体
  "payloadDigest": "..."   // payload 的 SHA-256（用于去重/比对）
}
```

#### `stage: "llm_response"` — LLM 响应

LLM 返回完整流式响应后写入，包含所有 SSE 事件和最终 AssistantMessage。

```jsonc
{
  "ts": "2026-03-06T10:00:05.000Z",
  "stage": "llm_response",
  "runId": "...",
  "callIndex": 1,           // 与对应 request 的 callIndex 相同
  "sseEvents": [ ... ],     // 所有 SSE 事件（delta 事件已去除 partial 快照以节省空间）
  "responsePayload": { ... },// 最终 AssistantMessage 对象
  "responseDurationMs": 5230 // 本次 LLM 调用耗时（毫秒）
}
```

SSE 事件类型包括：`start`、`text_start/delta/end`、`thinking_start/delta/end`、`toolcall_start/delta/end`、`done`、`error`。

#### `stage: "completion"` — Agent Run 结束

整个 Agent Run（含所有工具调用和多轮 LLM 调用）结束后写入。

```jsonc
{
  "ts": "2026-03-06T10:00:10.000Z",
  "stage": "completion",
  "runId": "...",
  "response": {
    "text": "助手最终文字回复",
    "thinking": "思考过程（如有）",
    "toolCalls": [ ... ]   // 最后一轮的工具调用
  },
  "usage": {               // Token 用量统计
    "input": 1234,
    "output": 567,
    "cacheRead": 800,
    "cacheWrite": 200,
    "totalTokens": 2801,
    "cost": { ... }
  },
  "error": null,           // 若 run 出错则为错误信息
  "durationMs": 10230      // 整个 Agent Run 耗时（毫秒）
}
```

### 日志实现位置

核心实现：`src/agents/llm-api-logger.ts`

调用入口：`src/agents/pi-embedded-runner/run/attempt.ts`

日志通过 `wrapStreamFn` 拦截每次 LLM 流式 API 调用，在不影响正常流程的情况下异步写入日志文件。

---

## 四、启动日志查看器

日志查看器是一个独立的 Python/Flask 应用，位于 `./openclaw-log-viewer/`，监听端口 **5001**。

### 快速启动

```bash
cd openclaw-log-viewer
./start.sh
```

脚本会自动创建 Python 虚拟环境并安装依赖，随后启动服务。

### 手动启动

```bash
cd openclaw-log-viewer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

### 访问

启动后打开浏览器访问：[http://127.0.0.1:5001](http://127.0.0.1:5001)

详细使用说明见 `./openclaw-log-viewer/README.md`。
