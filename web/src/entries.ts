/**
 * 事件 -> 界面状态。
 *
 * 这里和后端 `session/projection.py` 是同一个东西的两份实现，关系值得说清楚：
 *
 * 后端那份把**事件日志**投影成**消息列表**，是唯一事实源。
 * 这份把**消息列表**（或实时事件流）投影成**界面上的气泡和卡片**，是可丢的。
 *
 * 之所以两条路（刷新后读历史 / 实时收事件）都走这里，是为了让它们给出同一种形状。
 * 分开写的话，刷新一次页面看到的东西和刚才流式时不一样，而这种 bug
 * 只在「跑一半刷新」时才出现，最容易漏测。
 */

import type { Entry, MessageOut, StreamEvent, TurnMeta } from "./types";

/** 与后端 `projection.ERROR_PREFIX` 一致：工具失败时它会给内容加这个前缀。 */
export const TOOL_ERROR_PREFIX = "[tool error] ";

let counter = 0;
function nextId(): string {
  counter += 1;
  return `e${counter}`;
}

/** 供测试与「换会话」时重置，避免 id 无上限增长。 */
export function resetIds(): void {
  counter = 0;
}

function toolEntry(callId: string, name: string, args: string, replayed: boolean): Entry {
  return {
    kind: "tool",
    id: nextId(),
    callId,
    name,
    args,
    status: "running",
    content: "",
    durationMs: null,
    replayed,
  };
}

/**
 * 从历史消息重建界面状态。
 *
 * 工具调用在日志里是两条事件（`tool/call` + `tool/result`），在界面上是一张卡片：
 * 前一条给出参数并让卡片进入「执行中」，后一条给出结果。
 * 只有 `tool/call` 没有 `tool/result` 就是「已受理、结果未知」——
 * 那种卡片会一直停在执行中，这正是恢复面板要提示的状态。
 */
export function fromHistory(messages: MessageOut[]): Entry[] {
  const entries: Entry[] = [];
  const callIndex = new Map<string, number>();

  for (const message of messages) {
    if (message.role === "user") {
      entries.push({ kind: "user", id: nextId(), text: message.content });
      continue;
    }

    if (message.role === "assistant") {
      // 空内容的 assistant 消息是「只发工具调用、不说话」的那一轮。
      // 实时流里它本来也没产生任何气泡，回放时同样不该冒出一个空气泡。
      if (message.content !== "") {
        entries.push({
          kind: "assistant",
          id: nextId(),
          text: message.content,
          streaming: false,
        });
      }
      for (const call of message.tool_calls) {
        callIndex.set(call.id, entries.length);
        entries.push(toolEntry(call.id, call.name, call.arguments, true));
      }
      continue;
    }

    if (message.role === "tool") {
      const isError = message.content.startsWith(TOOL_ERROR_PREFIX);
      const text = isError ? message.content.slice(TOOL_ERROR_PREFIX.length) : message.content;
      const index = message.tool_call_id === null ? undefined : callIndex.get(message.tool_call_id);

      if (index === undefined) {
        // 结果找不到对应的调用。后端的投影层会把它记成 anomaly，
        // 界面上也如实显示 —— 静默丢掉会让日志里的异常永远没人发现。
        entries.push({ kind: "error", id: nextId(), text: `孤立的结果：${text}` });
        continue;
      }

      const previous = entries[index];
      if (previous !== undefined && previous.kind === "tool") {
        entries[index] = { ...previous, status: isError ? "error" : "ok", content: text };
      }
    }
  }

  return entries;
}

/**
 * 把一条实时事件并进界面状态，返回新的数组。
 *
 * 文本分片累积到当前气泡里；一旦出现工具调用，这个气泡就算说完了，
 * 之后的文本会开一个新气泡。于是界面的分段和后端日志里的
 * `assistant/message` 分段是对齐的 —— 后端也是每个模型往返记一条。
 */
export function applyEvent(entries: Entry[], event: StreamEvent): Entry[] {
  switch (event.type) {
    case "text": {
      const last = entries[entries.length - 1];
      if (last !== undefined && last.kind === "assistant" && last.streaming) {
        const updated: Entry = { ...last, text: last.text + event.text };
        return [...entries.slice(0, -1), updated];
      }
      return [...entries, { kind: "assistant", id: nextId(), text: event.text, streaming: true }];
    }

    case "tool_started":
      return [...entries, toolEntry(event.call_id, event.name, event.arguments, false)];

    case "tool_finished": {
      // 从后往前找：同一轮里可能有多次调用，最近的那个才是这次的结果
      for (let i = entries.length - 1; i >= 0; i -= 1) {
        const entry = entries[i];
        if (entry !== undefined && entry.kind === "tool" && entry.callId === event.call_id) {
          const updated: Entry = {
            ...entry,
            status: event.is_error ? "error" : "ok",
            content: event.content,
            durationMs: event.duration_ms,
          };
          return [...entries.slice(0, i), updated, ...entries.slice(i + 1)];
        }
      }
      return entries;
    }

    case "done":
      return entries.map((entry) =>
        entry.kind === "assistant" && entry.streaming ? { ...entry, streaming: false } : entry,
      );

    case "error":
      return [...entries, { kind: "error", id: nextId(), text: `${event.kind}：${event.message}` }];
  }
}

export function metaFrom(event: Extract<StreamEvent, { type: "done" }>): TurnMeta {
  return {
    rounds: event.rounds,
    toolCalls: event.tool_calls,
    stopReason: event.stop_reason,
  };
}
