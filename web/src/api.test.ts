import { describe, expect, it } from "vitest";

import { parseFrame, parseFrames } from "./api";

describe("parseFrames", () => {
  it("切出完整帧，并把没结束的残片留在 buffer 里", () => {
    const { frames, rest } = parseFrames("event: text\ndata: {}\n\nevent: don");

    expect(frames).toEqual(["event: text\ndata: {}"]);
    expect(rest).toBe("event: don");
  });

  it("一次给出多帧，最后一帧没以空行收尾就留着", () => {
    // 分帧符是空行，所以末尾那条没有空行的**不完整** —— 它可能还被截着。
    // 只有当流结束了（runTurn 的最后一步）才该当成完整帧处理。
    const { frames, rest } = parseFrames(
      "event: a\ndata: {}\n\nevent: b\ndata: {}\n\nevent: c\ndata: {}",
    );

    expect(frames).toEqual(["event: a\ndata: {}", "event: b\ndata: {}"]);
    expect(rest).toBe("event: c\ndata: {}");
  });

  it("容忍 \\r\\n —— 中间隔着代理时行尾可能被改写", () => {
    const { frames, rest } = parseFrames("event: text\r\ndata: {}\r\n\r\n");

    expect(frames).toEqual(["event: text\r\ndata: {}"]);
    expect(rest).toBe("");
  });

  it("没有完整帧时一帧都不返回", () => {
    const { frames, rest } = parseFrames("event: text\ndata: {\"text\":\"半");

    expect(frames).toEqual([]);
    expect(rest).toBe("event: text\ndata: {\"text\":\"半");
  });

  it("把一条帧拆成多块再拼起来，结果和一次给全一样", () => {
    // 网络不保证一条帧一次到达。这是手动解 SSE 最容易写错的地方：
    // 切早了 JSON 被截断，清空了事件就丢了。
    const frame = 'event: text\ndata: {"text":"你好"}\n\n';
    const buffer = frame.slice(0, 12);
    const { frames, rest } = parseFrames(buffer);

    expect(frames).toEqual([]);
    expect(parseFrames(rest + frame.slice(12)).frames).toEqual([
      'event: text\ndata: {"text":"你好"}',
    ]);
  });
});

describe("parseFrame", () => {
  it("按事件名解出对应的形状", () => {
    expect(parseFrame('event: text\ndata: {"text":"好"}')).toEqual({ type: "text", text: "好" });
  });

  it("带上工具参数", () => {
    expect(
      parseFrame('event: tool_started\ndata: {"call_id":"c1","name":"echo","arguments":"{}"}'),
    ).toEqual({ type: "tool_started", call_id: "c1", name: "echo", arguments: "{}" });
  });

  it("忽略心跳注释行", () => {
    expect(parseFrame(": keep-alive\nevent: text\ndata: {\"text\":\"好\"}")).toEqual({
      type: "text",
      text: "好",
    });
  });

  it("没有 data 行就返回 null", () => {
    expect(parseFrame("event: text")).toBeNull();
  });

  it("认不出的事件名不静默丢掉，转成一个 error 事件", () => {
    // 后端加了新事件而前端没跟上时，界面要能看出来，
    // 而不是安静地少显示点东西
    const event = parseFrame('event: something_new\ndata: {"x":1}');

    expect(event?.type).toBe("error");
    expect(event).toMatchObject({ kind: "unknown_event" });
  });

  it("多行 data 按规范用换行拼接", () => {
    expect(parseFrame('event: text\ndata: {"text":\ndata: "两行"}')).toEqual({
      type: "text",
      text: "两行",
    });
  });

  it("done 事件的 unfinished 缺省是空数组", () => {
    const event = parseFrame('event: done\ndata: {"text":"","rounds":2,"tool_calls":1}');

    expect(event).toMatchObject({ type: "done", rounds: 2, tool_calls: 1, unfinished: [] });
  });
});
