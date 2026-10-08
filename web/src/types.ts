/**
 * 与后端 SSE 事件一一对应。
 *
 * 事件名和字段名都不是自己起的，来自 `api/app.py` 的 `_encode()`。
 * 两边对不上时这里不会报错，只会安静地少显示点东西 —— 所以改动那三个
 * dataclass（`TextChunk` / `ToolStarted` / `ToolFinished`）时要回来改这里。
 */

export type StreamEvent =
  | { type: "text"; text: string }
  | { type: "tool_started"; call_id: string; name: string; arguments: string }
  | {
      type: "tool_finished";
      call_id: string;
      name: string;
      is_error: boolean;
      duration_ms: number;
      content: string;
    }
  | {
      type: "done";
      text: string;
      rounds: number;
      tool_calls: number;
      stop_reason: string;
      unfinished: string[];
    }
  | { type: "error"; kind: string; message: string };

export interface ToolCallOut {
  id: string;
  name: string;
  arguments: string;
}

export interface MessageOut {
  role: string;
  content: string;
  tool_call_id: string | null;
  tool_calls: ToolCallOut[];
}

export interface UnfinishedCall {
  call_id: string;
  name: string;
  arguments: string;
}

export interface HealthOut {
  status: string;
  provider: string;
  model: string;
  tools: string[];
}

/**
 * 界面状态。
 *
 * 和事件日志之间是「投影」的关系，不是一一对应：一次工具调用在日志里是两条
 * 事件（``tool/call`` + ``tool/result``），在界面上是一张卡片，
 * 卡片的状态由后者决定。所以 `fromHistory` 和实时流两条路必须产出同一种形状，
 * 否则刷新页面后看到的东西会和流式时不一样。
 */
export type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; streaming: boolean }
  | {
      kind: "tool";
      id: string;
      callId: string;
      name: string;
      args: string;
      status: "running" | "ok" | "error";
      content: string;
      durationMs: number | null;
      /** 从日志回放出来的卡片没有耗时 —— 日志里没记。界面要如实显示这一点。 */
      replayed: boolean;
    }
  | { kind: "error"; id: string; text: string };

export interface TurnMeta {
  rounds: number;
  toolCalls: number;
  stopReason: string;
}
