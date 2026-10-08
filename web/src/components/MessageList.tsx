import { useEffect, useRef } from "react";

import type { Entry, TurnMeta } from "../types";
import { ToolCallCard } from "./ToolCallCard";

function EntryView({ entry }: { entry: Entry }) {
  switch (entry.kind) {
    case "user":
      return <div className="bubble bubble--user">{entry.text}</div>;

    case "assistant":
      return (
        <div className="bubble bubble--assistant">
          {entry.text}
          {entry.streaming && <span className="caret" aria-label="正在生成" />}
        </div>
      );

    case "tool":
      return <ToolCallCard entry={entry} />;

    case "error":
      return <div className="bubble bubble--error">{entry.text}</div>;
  }
}

export function MessageList({
  entries,
  meta,
  busy,
}: {
  entries: Entry[];
  meta: TurnMeta | null;
  busy: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  // 贴着底部。用户往上翻看历史时不该被强行拽回来，
  // 所以只在本来就接近底部时才自动滚。
  useEffect(() => {
    const node = endRef.current;
    if (node === null) return;
    node.scrollIntoView({ block: "end" });
  }, [entries]);

  if (entries.length === 0) {
    return (
      <div className="empty">
        <p>还没有消息。</p>
        <p className="empty__hint">
          发一句话试试 —— 模型会自己决定要不要调用工具，工具调用会在这里以卡片形式出现。
        </p>
      </div>
    );
  }

  return (
    <div className="transcript">
      {entries.map((entry) => (
        <EntryView key={entry.id} entry={entry} />
      ))}

      {meta !== null && !busy && (
        <div className="turnmeta">
          本轮 {meta.rounds} 次模型往返、{meta.toolCalls} 次工具调用，结束原因{" "}
          <code>{meta.stopReason}</code>
        </div>
      )}

      <div ref={endRef} />
    </div>
  );
}
