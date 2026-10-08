import { beforeEach, describe, expect, it } from "vitest";

import { applyEvent, fromHistory, resetIds } from "./entries";
import type { Entry, MessageOut, StreamEvent } from "./types";

beforeEach(resetIds);

const assistantWithCall = (id: string, name: string, args: string): MessageOut => ({
  role: "assistant",
  content: "让我查一下。",
  tool_call_id: null,
  tool_calls: [{ id, name, arguments: args }],
});

const toolResult = (callId: string, content: string): MessageOut => ({
  role: "tool",
  content,
  tool_call_id: callId,
  tool_calls: [],
});

const userMessage = (text: string): MessageOut => ({
  role: "user",
  content: text,
  tool_call_id: null,
  tool_calls: [],
});

function kinds(entries: Entry[]): string[] {
  return entries.map((entry) => entry.kind);
}

describe("fromHistory", () => {
  it("把 user / assistant / tool 还原成气泡与卡片", () => {
    const entries = fromHistory([
      userMessage("现在几点"),
      assistantWithCall("c1", "now", '{"utc_offset_hours":8}'),
      toolResult("c1", "2026-10-08T16:00:00+08:00"),
      { role: "assistant", content: "现在是下午四点。", tool_call_id: null, tool_calls: [] },
    ]);

    expect(kinds(entries)).toEqual(["user", "assistant", "tool", "assistant"]);

    const tool = entries[2];
    expect(tool).toMatchObject({ kind: "tool", callId: "c1", name: "now", status: "ok" });
    expect(tool).toMatchObject({ content: "2026-10-08T16:00:00+08:00" });
  });

  it("只有 tool/call 没有 tool/result 时，卡片停在执行中", () => {
    // 这就是「已受理、结果未知」，恢复面板要提示的状态。
    // 回放时必须还原成执行中，不能当成成功或失败。
    const entries = fromHistory([
      userMessage("写个文件"),
      assistantWithCall("c1", "write_note", '{"path":"a.md"}'),
    ]);

    expect(entries[2]).toMatchObject({ kind: "tool", status: "running", content: "" });
  });

  it("失败的结果去掉前缀、标成 error", () => {
    const entries = fromHistory([
      userMessage("读个不存在的文件"),
      assistantWithCall("c1", "read_note", '{"path":"nope.md"}'),
      toolResult("c1", "[tool error] 文件不存在"),
    ]);

    expect(entries[2]).toMatchObject({ kind: "tool", status: "error", content: "文件不存在" });
  });

  it("跳过空内容的 assistant 消息", () => {
    // 只发工具调用不说话的那一轮，实时流里本来就没有气泡。
    // 回放时若给它补一个空气泡，刷新前后看到的东西就不一样了。
    const entries = fromHistory([
      userMessage("现在几点"),
      { role: "assistant", content: "", tool_call_id: null, tool_calls: [] },
    ]);

    expect(kinds(entries)).toEqual(["user"]);
  });

  it("找不到对应调用的结果不静默丢掉", () => {
    const entries = fromHistory([userMessage("x"), toolResult("ghost", "莫名其妙的结果")]);

    expect(kinds(entries)).toContain("error");
    expect(entries.at(-1)).toMatchObject({ kind: "error" });
  });

  it("从日志回放的卡片没有耗时 —— 日志里没这个字段", () => {
    const entries = fromHistory([
      userMessage("现在几点"),
      assistantWithCall("c1", "now", "{}"),
      toolResult("c1", "ok"),
    ]);

    expect(entries[2]).toMatchObject({ durationMs: null, replayed: true });
  });
});

describe("applyEvent", () => {
  it("文本分片累积到同一个气泡里", () => {
    let entries: Entry[] = [];
    entries = applyEvent(entries, { type: "text", text: "你" });
    entries = applyEvent(entries, { type: "text", text: "好" });

    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ kind: "assistant", text: "你好", streaming: true });
  });

  it("工具调用之后的文本开新气泡，分段与后端的日志对齐", () => {
    // 后端每个模型往返记一条 assistant/message。界面若把所有文本
    // 攒进同一个气泡，刷新一次就会看到不一样的分段。
    let entries: Entry[] = [];
    entries = applyEvent(entries, { type: "text", text: "让我查一下。" });
    entries = applyEvent(entries, {
      type: "tool_started",
      call_id: "c1",
      name: "now",
      arguments: "{}",
    });
    entries = applyEvent(entries, { type: "text", text: "现在是四点。" });

    expect(kinds(entries)).toEqual(["assistant", "tool", "assistant"]);
    expect(entries[0]).toMatchObject({ text: "让我查一下。" });
    expect(entries[2]).toMatchObject({ text: "现在是四点。" });
  });

  it("tool_finished 按 call_id 落到对应的卡片上", () => {
    let entries: Entry[] = [];
    for (const id of ["c1", "c2"]) {
      entries = applyEvent(entries, {
        type: "tool_started",
        call_id: id,
        name: "echo",
        arguments: "{}",
      });
    }
    entries = applyEvent(entries, {
      type: "tool_finished",
      call_id: "c1",
      name: "echo",
      is_error: false,
      duration_ms: 12,
      content: "ok",
    });

    expect(entries[0]).toMatchObject({ callId: "c1", status: "ok", durationMs: 12 });
    expect(entries[1]).toMatchObject({ callId: "c2", status: "running" });
  });

  it("done 结束所有气泡的流式状态", () => {
    let entries: Entry[] = [{ kind: "assistant", id: "a", text: "好", streaming: true }];
    entries = applyEvent(entries, {
      type: "done",
      text: "好",
      rounds: 1,
      tool_calls: 0,
      stop_reason: "stop",
      unfinished: [],
    });

    expect(entries[0]).toMatchObject({ streaming: false });
  });

  it("error 事件变成一个可见的气泡", () => {
    const entries = applyEvent([], {
      type: "error",
      kind: "provider",
      message: "上游 429",
    } as StreamEvent);

    expect(entries[0]).toMatchObject({ kind: "error" });
    expect(entries[0]).toMatchObject({ text: "provider：上游 429" });
  });

  it("原数组不被改动 —— React 靠引用变化决定重渲染", () => {
    const before: Entry[] = [];
    const after = applyEvent(before, { type: "text", text: "x" });

    expect(before).toHaveLength(0);
    expect(after).toHaveLength(1);
  });
});
