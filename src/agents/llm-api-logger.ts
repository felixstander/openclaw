import crypto from "node:crypto";
import path from "node:path";
import type { AgentMessage, StreamFn } from "@mariozechner/pi-agent-core";
import type { AssistantMessageEvent } from "@mariozechner/pi-ai";
import { resolveStateDir } from "../config/paths.js";
import { createSubsystemLogger } from "../logging/subsystem.js";
import { resolveUserPath } from "../utils.js";
import { parseBooleanValue } from "../utils/boolean.js";
import { safeJsonStringify } from "../utils/safe-json.js";
import { getQueuedFileWriter, type QueuedFileWriter } from "./queued-file-writer.js";

type LlmApiLogEntry = {
  ts: string;
  stage: "request" | "llm_response" | "completion";
  runId?: string;
  sessionId?: string;
  sessionKey?: string;
  provider?: string;
  modelId?: string;
  modelApi?: string | null;
  workspaceDir?: string;
  // "request" stage: raw payload sent to the LLM API
  payload?: unknown;
  payloadDigest?: string;
  // "llm_response" stage: per-call SSE events + final AssistantMessage
  callIndex?: number;
  sseEvents?: AssistantMessageEvent[];
  responsePayload?: unknown;
  responseError?: string;
  responseDurationMs?: number;
  // "completion" stage: extracted response and usage from final messages
  response?: {
    text?: string;
    toolCalls?: Array<{ id?: string; name?: string; input?: unknown }>;
    thinking?: string;
  };
  usage?: Record<string, unknown>;
  error?: string;
  durationMs?: number;
};

const writers = new Map<string, QueuedFileWriter>();
const log = createSubsystemLogger("agent/llm-api-logger");

function resolveLogConfig(env: NodeJS.ProcessEnv): { enabled: boolean; logDir: string } {
  const enabledStr = env.OPENCLAW_LLM_API_LOG;
  // Default to enabled; set OPENCLAW_LLM_API_LOG=0 to disable
  const enabled = enabledStr === undefined ? true : (parseBooleanValue(enabledStr) ?? true);
  const dirOverride = env.OPENCLAW_LLM_API_LOG_DIR?.trim();
  const logDir = dirOverride
    ? resolveUserPath(dirOverride)
    : path.join(resolveStateDir(env), "logs", "llm-api");
  return { enabled, logDir };
}

function digest(value: unknown): string | undefined {
  const serialized = safeJsonStringify(value);
  if (!serialized) {
    return undefined;
  }
  return crypto.createHash("sha256").update(serialized).digest("hex");
}

function formatError(error: unknown): string | undefined {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  if (typeof error === "number" || typeof error === "boolean" || typeof error === "bigint") {
    return String(error);
  }
  if (error && typeof error === "object") {
    return safeJsonStringify(error) ?? "unknown error";
  }
  return undefined;
}

function findLastAssistantMessage(messages: AgentMessage[]): AgentMessage | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i] as { role?: unknown };
    if (msg?.role === "assistant") {
      return messages[i];
    }
  }
  return null;
}

function extractResponse(msg: AgentMessage): LlmApiLogEntry["response"] {
  const content = (msg as { content?: unknown }).content;
  if (!Array.isArray(content)) {
    return undefined;
  }

  const result: NonNullable<LlmApiLogEntry["response"]> = {};
  const textParts: string[] = [];
  const thinkingParts: string[] = [];
  const toolCalls: NonNullable<NonNullable<LlmApiLogEntry["response"]>["toolCalls"]> = [];

  for (const block of content) {
    if (!block || typeof block !== "object") {
      continue;
    }
    const b = block as Record<string, unknown>;
    if (b.type === "text" && typeof b.text === "string") {
      textParts.push(b.text);
    } else if (
      (b.type === "thinking" || b.type === "reasoning") &&
      typeof b.thinking === "string"
    ) {
      thinkingParts.push(b.thinking);
    } else if (b.type === "tool_use") {
      toolCalls.push({
        id: typeof b.id === "string" ? b.id : undefined,
        name: typeof b.name === "string" ? b.name : undefined,
        input: b.input,
      });
    }
  }

  if (textParts.length > 0) {
    result.text = textParts.join("\n");
  }
  if (thinkingParts.length > 0) {
    result.thinking = thinkingParts.join("\n");
  }
  if (toolCalls.length > 0) {
    result.toolCalls = toolCalls;
  }

  return Object.keys(result).length > 0 ? result : undefined;
}

export type LlmApiLogger = {
  wrapStreamFn: (streamFn: StreamFn) => StreamFn;
  recordCompletion: (messages: AgentMessage[], error?: unknown, startedAtMs?: number) => void;
};

export function createLlmApiLogger(params: {
  env?: NodeJS.ProcessEnv;
  runId?: string;
  sessionId?: string;
  sessionKey?: string;
  provider?: string;
  modelId?: string;
  modelApi?: string | null;
  workspaceDir?: string;
}): LlmApiLogger | null {
  const env = params.env ?? process.env;
  const cfg = resolveLogConfig(env);
  if (!cfg.enabled) {
    return null;
  }

  // One file per session (matching opencode's per-session approach)
  const sessionLabel = params.sessionId ?? params.runId;
  const fileName = sessionLabel
    ? `${sessionLabel}.ndjson`
    : `llm-api-${new Date().toISOString().slice(0, 10)}.ndjson`;
  const filePath = path.join(cfg.logDir, fileName);
  const writer = getQueuedFileWriter(writers, filePath);

  const base = {
    runId: params.runId,
    sessionId: params.sessionId,
    sessionKey: params.sessionKey,
    provider: params.provider,
    modelId: params.modelId,
    modelApi: params.modelApi,
    workspaceDir: params.workspaceDir,
  };

  const record = (entry: LlmApiLogEntry) => {
    const line = safeJsonStringify(entry);
    if (!line) {
      return;
    }
    writer.write(`${line}\n`);
  };

  // Per-run call counter so each request/response pair has a matching callIndex.
  let callIndex = 0;

  // Wrap the StreamFn to intercept the raw request payload and the full response stream.
  // Fires once per LLM API call within a run (including tool-use follow-up calls).
  const wrapStreamFn = (streamFn: StreamFn): StreamFn => {
    return (model, context, options) => {
      const thisCallIndex = ++callIndex;
      const callStartMs = Date.now();

      const nextOnPayload = (payload: unknown) => {
        record({
          ...base,
          ts: new Date().toISOString(),
          stage: "request",
          callIndex: thisCallIndex,
          payload,
          payloadDigest: digest(payload),
        });
        options?.onPayload?.(payload);
      };

      const originalStream = streamFn(model, context, {
        ...options,
        onPayload: nextOnPayload,
      });

      // Wrap the returned AsyncIterable to tap every SSE event + final message.
      // IMPORTANT: the agent-loop breaks out of `for await` immediately after receiving the
      // "done"/"error" event (it returns early). That means any code *after* the for-await in
      // this generator never runs. We must record the log *before* yielding the terminal event.
      const wrappedStream = (async function* () {
        const sseEvents: AssistantMessageEvent[] = [];
        try {
          for await (const event of originalStream) {
            // Collect a compact copy: strip large partial snapshots from delta events to keep
            // log size manageable. "done"/"error" events carry the full final message.
            const compact: AssistantMessageEvent =
              event.type === "text_delta" ||
              event.type === "thinking_delta" ||
              event.type === "toolcall_delta"
                ? { ...event, partial: undefined as never }
                : event;
            sseEvents.push(compact);

            // Record BEFORE yielding the terminal event. The agent-loop exits its for-await
            // immediately after "done"/"error", so post-loop code in this generator never runs.
            if (event.type === "done" || event.type === "error") {
              const responsePayload = event.type === "done" ? event.message : event.error;
              record({
                ...base,
                ts: new Date().toISOString(),
                stage: "llm_response",
                callIndex: thisCallIndex,
                sseEvents,
                responsePayload,
                responseDurationMs: Date.now() - callStartMs,
              });
            }

            yield event;
          }
        } catch (err) {
          record({
            ...base,
            ts: new Date().toISOString(),
            stage: "llm_response",
            callIndex: thisCallIndex,
            sseEvents,
            responseError: formatError(err),
            responseDurationMs: Date.now() - callStartMs,
          });
          throw err;
        }
      })();

      // Preserve the .result() method from AssistantMessageEventStream so callers
      // that await .result() still get the final AssistantMessage correctly.
      (wrappedStream as unknown as { result: () => Promise<unknown> }).result =
        originalStream.result.bind(originalStream);

      return wrappedStream as ReturnType<StreamFn>;
    };
  };

  // Record the final completion after the entire agent run finishes.
  // Extracts text/tool calls from the last assistant message and usage stats.
  const recordCompletion = (messages: AgentMessage[], error?: unknown, startedAtMs?: number) => {
    const lastAssistant = findLastAssistantMessage(messages);
    const response = lastAssistant ? extractResponse(lastAssistant) : undefined;
    const usage = lastAssistant
      ? (lastAssistant as { usage?: Record<string, unknown> }).usage
      : undefined;

    record({
      ...base,
      ts: new Date().toISOString(),
      stage: "completion",
      response,
      usage: usage,
      error: formatError(error),
      durationMs: startedAtMs !== undefined ? Date.now() - startedAtMs : undefined,
    });

    log.info("llm api run recorded", {
      runId: params.runId,
      sessionId: params.sessionId,
      provider: params.provider,
      modelId: params.modelId,
    });
  };

  log.info("llm api logger enabled", { filePath: writer.filePath });
  return { wrapStreamFn, recordCompletion };
}
