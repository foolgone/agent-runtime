/**
 * 后端接口。
 *
 * 对话那条路用 `fetch` + ReadableStream 手动解 SSE，而不是 `EventSource`：
 * EventSource 只支持 GET 且不能带请求体，而 `/sessions/{id}/messages` 是 POST
 * （消息在 body 里）。换 GET 把输入塞进 query string 会撞上 URL 长度限制，
 * 也会把用户输入留在访问日志里。
 *
 * 代价是要自己处理分帧。见 `parseFrames`。
 */

import type { HealthOut, MessageOut, StreamEvent, UnfinishedCall } from "./types";

const JSON_HEADERS = { "Content-Type": "application/json" };

async function fail(response: Response, what: string): Promise<never> {
  let detail = "";
  try {
    const body = (await response.json()) as { detail?: unknown };
    detail = typeof body.detail === "string" ? body.detail : "";
  } catch {
    // 响应体不是 JSON（502 之类的网关错误），保留下面的状态码就够了
  }
  throw new Error(`${what}失败：${response.status}${detail ? ` ${detail}` : ""}`);
}

export async function fetchHealth(): Promise<HealthOut> {
  const response = await fetch("/healthz");
  if (!response.ok) await fail(response, "读取服务状态");
  return (await response.json()) as HealthOut;
}

export async function listSessions(): Promise<string[]> {
  const response = await fetch("/sessions");
  if (!response.ok) await fail(response, "读取会话列表");
  return ((await response.json()) as { sessions: string[] }).sessions;
}

export async function createSession(): Promise<string> {
  const response = await fetch("/sessions", { method: "POST" });
  if (!response.ok) await fail(response, "新建会话");
  return ((await response.json()) as { session_id: string }).session_id;
}

export async function fetchHistory(sessionId: string): Promise<MessageOut[]> {
  const response = await fetch(`/sessions/${encodeURIComponent(sessionId)}/messages`);
  if (!response.ok) await fail(response, "读取历史");
  return ((await response.json()) as { messages: MessageOut[] }).messages;
}

export async function fetchRecovery(sessionId: string): Promise<UnfinishedCall[]> {
  const response = await fetch(`/sessions/${encodeURIComponent(sessionId)}/recovery`);
  if (!response.ok) await fail(response, "读取恢复状态");
  return ((await response.json()) as { unfinished_calls: UnfinishedCall[] }).unfinished_calls;
}

/**
 * 把一块 buffer 切成完整的帧，返回帧和剩下的残片。
 *
 * SSE 的分帧符是空行。网络不保证一条帧一次到达 —— 它可能一条帧拆成三块，
 * 也可能三块里装着两条半。所以每次读到数据只能把 buffer 拼起来，
 * 再从中切出**完整**的帧；最后那半条必须留着等下一块，
 * 切早了会把 JSON 截断，清空了会丢事件。
 */
export function parseFrames(buffer: string): { frames: string[]; rest: string } {
  const frames: string[] = [];
  // 按 \n\n 切，但容忍 \r\n —— 中间隔着代理时行尾可能被改写
  const pattern = /\r?\n\r?\n/;

  let rest = buffer;
  for (;;) {
    const match = pattern.exec(rest);
    if (match === null) break;
    frames.push(rest.slice(0, match.index));
    rest = rest.slice(match.index + match[0].length);
  }
  return { frames, rest };
}

/** 一条帧 -> 一个事件。注释行（心跳）和空 data 返回 null。 */
export function parseFrame(frame: string): StreamEvent | null {
  let name = "";
  const dataLines: string[] = [];

  for (const line of frame.split(/\r?\n/)) {
    if (line === "" || line.startsWith(":")) continue; // 空行 / 心跳注释
    if (line.startsWith("event:")) {
      name = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).replace(/^ /, ""));
    }
  }

  if (name === "" || dataLines.length === 0) return null;

  // 多个 data 行按 SSE 规范用 \n 拼接
  const payload = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;

  switch (name) {
    case "text":
      return { type: "text", text: String(payload.text ?? "") };
    case "tool_started":
      return {
        type: "tool_started",
        call_id: String(payload.call_id ?? ""),
        name: String(payload.name ?? ""),
        arguments: String(payload.arguments ?? ""),
      };
    case "tool_finished":
      return {
        type: "tool_finished",
        call_id: String(payload.call_id ?? ""),
        name: String(payload.name ?? ""),
        is_error: Boolean(payload.is_error),
        duration_ms: Number(payload.duration_ms ?? 0),
        content: String(payload.content ?? ""),
      };
    case "done":
      return {
        type: "done",
        text: String(payload.text ?? ""),
        rounds: Number(payload.rounds ?? 0),
        tool_calls: Number(payload.tool_calls ?? 0),
        stop_reason: String(payload.stop_reason ?? ""),
        unfinished: Array.isArray(payload.unfinished) ? payload.unfinished.map(String) : [],
      };
    case "error":
      return {
        type: "error",
        kind: String(payload.kind ?? "internal"),
        message: String(payload.message ?? ""),
      };
    default:
      // 后端加了新事件而这里没跟上时，不要在界面上装作无事发生
      return { type: "error", kind: "unknown_event", message: `未知事件：${name}` };
  }
}

/**
 * 跑一轮对话，逐个吐出事件。
 *
 * 用 async generator 而不是回调：调用方可以 `for await` 直接写状态更新，
 * 不用把「这一轮结束了」拆成散落在几个回调里的判断。
 */
export async function* runTurn(
  sessionId: string,
  input: string,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ input }),
    signal,
  });

  if (!response.ok) await fail(response, "发送消息");
  if (response.body === null) throw new Error("响应没有 body，无法读取流");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      // stream: true —— 一个多字节字符可能正好被切在块的边界上
      buffer += decoder.decode(value, { stream: true });

      const { frames, rest } = parseFrames(buffer);
      buffer = rest;

      for (const frame of frames) {
        const event = parseFrame(frame);
        if (event !== null) yield event;
      }
    }

    // 刷掉解码器里剩下的字节，再处理没有以空行收尾的最后一帧
    buffer += decoder.decode();
    for (const frame of parseFrames(buffer).frames) {
      const event = parseFrame(frame);
      if (event !== null) yield event;
    }
  } finally {
    // 中途 return（用户点停止、组件卸载）时要把连接断开，
    // 否则后端只能等下次写的时候才发现对端没了
    await reader.cancel().catch(() => undefined);
  }
}
