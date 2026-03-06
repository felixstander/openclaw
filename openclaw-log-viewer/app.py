#!/usr/bin/env python3
"""
OpenClaw LLM API 日志查看器
读取 NDJSON 格式的 LLM API 日志并以美观的网页形式展示。
日志位置: ~/.openclaw/logs/llm-api/{sessionId}.ndjson
"""

import json
import os
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, jsonify
from markupsafe import Markup
import markdown
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.codehilite import CodeHiliteExtension

app = Flask(__name__)

# Markdown 渲染器
md = markdown.Markdown(extensions=[
    FencedCodeExtension(),
    CodeHiliteExtension(css_class='highlight'),
    'tables',
    'nl2br',
])

# 日志目录：~/.openclaw/logs/llm-api/
LOG_DIR = Path.home() / ".openclaw" / "logs" / "llm-api"


def get_log_files():
    """获取所有日志文件，按修改时间倒序排列"""
    if not LOG_DIR.exists():
        return []
    files = []
    for f in sorted(LOG_DIR.glob("*.ndjson"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        stats = count_stats(f)
        files.append({
            "name": f.name,
            "session_id": f.stem,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "turns": stats["turns"],
            "llm_calls": stats["llm_calls"],
        })
    return files


def count_stats(filepath):
    """快速扫描文件，统计对话轮次（distinct runId）和 LLM 调用次数（request 条目）"""
    run_ids = set()
    llm_calls = 0
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = entry.get("runId") or entry.get("sessionId")
                if rid:
                    run_ids.add(rid)
                if entry.get("stage") == "request":
                    llm_calls += 1
    except Exception:
        pass
    return {"turns": len(run_ids), "llm_calls": llm_calls}


def parse_log_file(filepath):
    """解析 NDJSON 日志文件，返回按 runId 分组的数据结构"""
    runs = {}  # runId -> { meta, requests[], completion }
    run_order = []  # 保持 runId 出现顺序

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                run_id = entry.get("runId") or entry.get("sessionId") or "unknown"
                if run_id not in runs:
                    runs[run_id] = {
                        "run_id": run_id,
                        "session_id": entry.get("sessionId", ""),
                        "session_key": entry.get("sessionKey", ""),
                        "provider": entry.get("provider", ""),
                        "model_id": entry.get("modelId", ""),
                        "model_api": entry.get("modelApi", ""),
                        "workspace_dir": entry.get("workspaceDir", ""),
                        "first_ts": entry.get("ts", ""),
                        "requests": [],
                        "responses": [],
                        "completion": None,
                    }
                    run_order.append(run_id)

                stage = entry.get("stage", "")
                if stage == "request":
                    runs[run_id]["requests"].append({
                        "ts": entry.get("ts", ""),
                        "call_index": entry.get("callIndex"),
                        "payload": entry.get("payload"),
                        "payload_digest": entry.get("payloadDigest", ""),
                        "payload_json": safe_json(entry.get("payload")),
                    })
                elif stage == "llm_response":
                    runs[run_id]["responses"].append({
                        "ts": entry.get("ts", ""),
                        "call_index": entry.get("callIndex"),
                        "sse_events": entry.get("sseEvents") or [],
                        "response_payload": entry.get("responsePayload"),
                        "response_payload_json": safe_json(entry.get("responsePayload")),
                        "response_error": entry.get("responseError"),
                        "duration_ms": entry.get("responseDurationMs"),
                    })
                elif stage == "completion":
                    runs[run_id]["completion"] = {
                        "ts": entry.get("ts", ""),
                        "response": entry.get("response") or {},
                        "usage": entry.get("usage") or {},
                        "error": entry.get("error"),
                        "duration_ms": entry.get("durationMs"),
                    }
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")

    # 按时间顺序排列（最早的 run 在前，最新的在后）
    result = [runs[rid] for rid in run_order if rid in runs]
    return result


def safe_json(obj, indent=2):
    """安全地序列化 JSON，处理不可序列化对象"""
    try:
        return json.dumps(obj, indent=indent, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def fmt_duration(ms):
    """格式化毫秒数为可读字符串"""
    if ms is None:
        return "N/A"
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.2f}s"


def fmt_ts(ts_str):
    """格式化 ISO 时间戳"""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_str


def extract_user_message(payload):
    """从 request payload 中提取触发此 run 的用户消息（messages 数组中最后一条 role=user）"""
    if not payload or not isinstance(payload, dict):
        return None
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return None
    # 找最后一条 user 消息
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            # Anthropic format: [{type: "text", text: "..."}]
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(block["text"])
                    elif block.get("type") == "tool_result":
                        # 跳过工具结果块，只取纯文本
                        pass
            text = "\n".join(parts).strip()
            if text:
                return text
    return None


def extract_payload_summary(payload):
    """从 payload 中提取摘要信息"""
    if not payload or not isinstance(payload, dict):
        return {}
    msgs = payload.get("messages") or payload.get("contents") or []
    tools = payload.get("tools") or []
    system = payload.get("system") or payload.get("systemInstruction") or ""
    if isinstance(system, list):
        # Anthropic format: system is array of content blocks
        system_texts = [b.get("text", "") for b in system if isinstance(b, dict)]
        system = "\n".join(system_texts)
    return {
        "model": payload.get("model", ""),
        "messages_count": len(msgs) if isinstance(msgs, list) else 0,
        "tools_count": len(tools) if isinstance(tools, list) else 0,
        "max_tokens": payload.get("max_tokens"),
        "temperature": payload.get("temperature"),
        "system_preview": (system or "")[:200] if system else "",
        "stream": payload.get("stream"),
    }


@app.route("/")
def index():
    files = get_log_files()
    return render_template("index.html", files=files, log_dir=str(LOG_DIR))


@app.route("/session/<path:session_id>")
def view_session(session_id):
    filepath = LOG_DIR / f"{session_id}.ndjson"
    if not filepath.exists():
        return f"Session file not found: {filepath}", 404

    runs = parse_log_file(filepath)

    # 格式化用于模板
    formatted_runs = []
    for run in runs:
        completion = run.get("completion") or {}
        requests = run.get("requests") or []

        # Build a callIndex -> response lookup for pairing
        responses = run.get("responses") or []
        response_by_call = {}
        for resp in responses:
            ci = resp.get("call_index")
            if ci is not None:
                response_by_call[ci] = resp

        formatted_requests = []
        for i, req in enumerate(requests):
            summary = extract_payload_summary(req.get("payload"))
            call_idx = req.get("call_index") or (i + 1)
            paired_resp = response_by_call.get(call_idx)

            # Build compact SSE event list (skip large deltas, keep structural events)
            sse_summary = []
            if paired_resp:
                for ev in (paired_resp.get("sse_events") or []):
                    ev_type = ev.get("type", "")
                    item = {"type": ev_type}
                    if ev_type == "text_delta":
                        item["delta"] = ev.get("delta", "")
                    elif ev_type == "thinking_delta":
                        item["delta"] = ev.get("delta", "")
                    elif ev_type == "toolcall_end":
                        tc = ev.get("toolCall") or {}
                        item["toolCall"] = {"name": tc.get("name"), "id": tc.get("id")}
                    elif ev_type in ("done", "error"):
                        item["reason"] = ev.get("reason", "")
                    sse_summary.append(item)

            formatted_requests.append({
                "index": i + 1,
                "call_index": call_idx,
                "ts": fmt_ts(req.get("ts", "")),
                "payload_json": req.get("payload_json", "null"),
                "payload_digest": req.get("payload_digest", ""),
                "summary": summary,
                # Response fields
                "has_response": paired_resp is not None,
                "response_ts": fmt_ts(paired_resp.get("ts", "")) if paired_resp else "",
                "response_duration": fmt_duration(paired_resp.get("duration_ms")) if paired_resp else "",
                "response_error": paired_resp.get("response_error") if paired_resp else None,
                "response_payload_json": paired_resp.get("response_payload_json", "null") if paired_resp else "null",
                "sse_events_json": safe_json(sse_summary),
                "sse_events_full_json": safe_json(paired_resp.get("sse_events") or []) if paired_resp else "[]",
                "sse_event_count": len(paired_resp.get("sse_events") or []) if paired_resp else 0,
            })

        response = completion.get("response") or {}
        usage = completion.get("usage") or {}

        # 从第一次 LLM 请求的 payload 里提取触发此 run 的用户消息
        first_payload = requests[0].get("payload") if requests else None
        user_message = extract_user_message(first_payload)

        formatted_runs.append({
            "run_id": run["run_id"],
            "session_id": run["session_id"],
            "provider": run["provider"],
            "model_id": run["model_id"],
            "model_api": run["model_api"],
            "first_ts": fmt_ts(run["first_ts"]),
            "requests": formatted_requests,
            "llm_call_count": len(formatted_requests),
            "completion_ts": fmt_ts(completion.get("ts", "")),
            "duration": fmt_duration(completion.get("duration_ms")),
            "error": completion.get("error"),
            "user_message": user_message,
            "response_text": response.get("text", ""),
            "response_thinking": response.get("thinking", ""),
            "response_tool_calls": response.get("toolCalls") or [],
            "usage": usage,
            "usage_json": safe_json(usage),
            "response_tool_calls_json": safe_json(response.get("toolCalls") or []),
        })

    return render_template(
        "session.html",
        runs=formatted_runs,
        session_id=session_id,
        total_runs=len(formatted_runs),
    )


@app.route("/api/sessions")
def api_sessions():
    return jsonify(get_log_files())


@app.route("/api/session/<path:session_id>")
def api_session(session_id):
    filepath = LOG_DIR / f"{session_id}.ndjson"
    if not filepath.exists():
        return jsonify({"error": "not found"}), 404
    runs = parse_log_file(filepath)
    return jsonify(runs)


@app.template_filter("markdown")
def markdown_filter(text):
    if not text:
        return ""
    md.reset()
    return Markup(md.convert(str(text)))


@app.template_filter("safe_json_filter")
def safe_json_filter(obj):
    return Markup(safe_json(obj))


if __name__ == "__main__":
    print(f"OpenClaw LLM API 日志查看器")
    print(f"日志目录: {LOG_DIR}")
    print(f"访问地址: http://127.0.0.1:5001")
    app.run(debug=True, host="127.0.0.1", port=5001)
