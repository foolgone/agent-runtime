import type { Entry } from "../types";

/** 参数是模型生成的 JSON 字符串，尽量排版一下；解析不了就原样显示，不要吞掉。 */
function prettyArguments(raw: string): string {
  if (raw.trim() === "") return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

const STATUS_LABEL: Record<"running" | "ok" | "error", string> = {
  running: "执行中",
  ok: "成功",
  error: "失败",
};

export function ToolCallCard({ entry }: { entry: Extract<Entry, { kind: "tool" }> }) {
  const args = prettyArguments(entry.args);

  return (
    <div className={`tool tool--${entry.status}`}>
      <div className="tool__head">
        <span className="tool__dot" aria-hidden="true" />
        <span className="tool__name">{entry.name}</span>
        <span className="tool__status">{STATUS_LABEL[entry.status]}</span>
        {entry.durationMs === null ? (
          // 从日志回放出来的卡片没有耗时：`tool/result` 事件里根本没记这个字段。
          // 显示成 0 会让人以为工具瞬间返回，那是在编造数据。
          <span className="tool__meta" title="日志里没有记录耗时，只有实时执行的那次才有">
            耗时未知
          </span>
        ) : (
          <span className="tool__meta">{entry.durationMs} ms</span>
        )}
      </div>

      {args !== "" && <pre className="tool__args">{args}</pre>}

      {entry.content !== "" && (
        <pre className={`tool__result ${entry.status === "error" ? "tool__result--error" : ""}`}>
          {entry.content}
        </pre>
      )}

      {entry.status === "running" && (
        <div className="tool__pending">
          已受理，结果未知 —— 执行中途可能已经产生副作用，重试前请先确认。
        </div>
      )}
    </div>
  );
}
