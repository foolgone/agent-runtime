import { useCallback, useEffect, useRef, useState } from "react";

import {
  createSession,
  fetchHealth,
  fetchHistory,
  fetchRecovery,
  listSessions,
  runTurn,
} from "./api";
import { MessageList } from "./components/MessageList";
import { RecoveryPanel } from "./components/RecoveryPanel";
import { applyEvent, fromHistory, metaFrom, resetIds } from "./entries";
import type { Entry, HealthOut, TurnMeta, UnfinishedCall } from "./types";

export default function App() {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [sessions, setSessions] = useState<string[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [meta, setMeta] = useState<TurnMeta | null>(null);
  const [unfinished, setUnfinished] = useState<UnfinishedCall[]>([]);
  const [recoveryLoading, setRecoveryLoading] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const refreshSessions = useCallback(async () => {
    setSessions(await listSessions());
  }, []);

  const refreshRecovery = useCallback(async (id: string) => {
    setRecoveryLoading(true);
    try {
      setUnfinished(await fetchRecovery(id));
    } catch {
      setUnfinished([]);
    } finally {
      setRecoveryLoading(false);
    }
  }, []);

  const openSession = useCallback(
    async (id: string) => {
      setBanner(null);
      setSessionId(id);
      setMeta(null);
      resetIds();
      try {
        const messages = await fetchHistory(id);
        setEntries(fromHistory(messages));
      } catch (error) {
        setEntries([]);
        setBanner(error instanceof Error ? error.message : String(error));
      }
      await refreshRecovery(id);
    },
    [refreshRecovery],
  );

  // 首屏：拿服务信息与已有会话。会话列表来自磁盘，所以刷新页面
  // （甚至后端重启）之后，之前的对话还在。
  useEffect(() => {
    void (async () => {
      try {
        setHealth(await fetchHealth());
      } catch (error) {
        setBanner(error instanceof Error ? error.message : String(error));
      }
      try {
        const existing = await listSessions();
        setSessions(existing);
        const first = existing[0];
        if (first !== undefined) await openSession(first);
      } catch (error) {
        setBanner(error instanceof Error ? error.message : String(error));
      }
    })();
  }, [openSession]);

  // 组件卸载时掐断还在跑的流，否则后端要等下一次写才发现对端没了
  useEffect(() => () => abortRef.current?.abort(), []);

  async function handleNewSession() {
    try {
      const id = await createSession();
      await refreshSessions();
      await openSession(id);
    } catch (error) {
      setBanner(error instanceof Error ? error.message : String(error));
    }
  }

  function handleStop() {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
  }

  async function handleSend() {
    const text = input.trim();
    if (text === "" || sessionId === null || busy) return;

    setInput("");
    setBanner(null);
    setMeta(null);
    setBusy(true);

    // 用户消息与模型回复一样是本地先画上去的：等后端回显会让输入框显得卡。
    // 后端在 run_turn 开头就把这条写进日志了，所以刷新后它还在。
    setEntries((previous) => [
      ...previous,
      { kind: "user", id: `local-${Date.now()}`, text },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const event of runTurn(sessionId, text, controller.signal)) {
        setEntries((previous) => applyEvent(previous, event));
        if (event.type === "done") setMeta(metaFrom(event));
      }
    } catch (error) {
      // 用户点停止触发的 abort 不是错误
      if (!controller.signal.aborted) {
        setBanner(error instanceof Error ? error.message : String(error));
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
      // 这一轮可能留下未收尾的调用（比如工具执行到一半断开了），
      // 每轮结束都重新问一次，别等用户手动刷新
      await refreshRecovery(sessionId);
      await refreshSessions();
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__name">agent-runtime</span>
          <span className="topbar__sub">「模型 → 工具 → 模型」循环</span>
        </div>
        {health !== null && (
          <div className="topbar__meta">
            <span className="chip">
              {health.provider} / {health.model}
            </span>
            <span className="chip chip--tools" title={health.tools.join("、")}>
              {health.tools.length} 个工具
            </span>
          </div>
        )}
      </header>

      <div className="body">
        <nav className="pane pane--sessions">
          <header className="pane__head">
            <h2>会话</h2>
            <button type="button" className="link" onClick={() => void handleNewSession()}>
              新建
            </button>
          </header>

          {sessions.length === 0 ? (
            <p className="pane__note">还没有会话。</p>
          ) : (
            <ul className="sessions">
              {sessions.map((id) => (
                <li key={id}>
                  <button
                    type="button"
                    className={`session ${id === sessionId ? "session--active" : ""}`}
                    onClick={() => void openSession(id)}
                  >
                    <code>{id}</code>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </nav>

        <main className="pane pane--chat">
          {banner !== null && <div className="banner">{banner}</div>}

          <MessageList entries={entries} meta={meta} busy={busy} />

          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSend();
            }}
          >
            <textarea
              value={input}
              placeholder={sessionId === null ? "先新建一个会话" : "说点什么，回车发送"}
              disabled={sessionId === null || busy}
              rows={2}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSend();
                }
              }}
            />
            {busy ? (
              <button type="button" className="btn btn--stop" onClick={handleStop}>
                停止
              </button>
            ) : (
              <button
                type="submit"
                className="btn"
                disabled={sessionId === null || input.trim() === ""}
              >
                发送
              </button>
            )}
          </form>
        </main>

        <RecoveryPanel
          calls={unfinished}
          loading={recoveryLoading}
          onRefresh={() => {
            if (sessionId !== null) void refreshRecovery(sessionId);
          }}
        />
      </div>
    </div>
  );
}
