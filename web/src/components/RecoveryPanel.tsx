import type { UnfinishedCall } from "../types";

/**
 * 恢复面板 —— 这个界面里唯一别处看不到的东西。
 *
 * 后端最值得一提的设计是「先记账、后执行」：工具调用在执行**之前**就先落盘，
 * 所以进程死在执行中途时，日志里会留下一条没有结果的记录。重放时那条记录
 * 会被识别成「已受理、结果未知」，而不是当它没发生过。
 *
 * 别的对话界面只能显示「哦，出错了」。这里能指出**具体哪一次调用**结果不明，
 * 因为日志里有这条记录。取消掉的那一轮不会凭空消失，这是可观测性，
 * 不是错误处理。
 */
export function RecoveryPanel({
  calls,
  loading,
  onRefresh,
}: {
  calls: UnfinishedCall[];
  loading: boolean;
  onRefresh: () => void;
}) {
  return (
    <aside className="pane pane--recovery">
      <header className="pane__head">
        <h2>未收尾的调用</h2>
        <button type="button" className="link" onClick={onRefresh} disabled={loading}>
          {loading ? "刷新中" : "刷新"}
        </button>
      </header>

      <p className="pane__note">
        工具调用在执行前先记账，所以进程中途死掉会留下没有结果的记录。下面这些
        <strong>结果未知</strong>，不能当作没发生过 —— 它可能已经产生了副作用。
      </p>

      {calls.length === 0 ? (
        <div className="ok-state">
          <span className="ok-state__dot" aria-hidden="true" />
          日志是干净的，没有未收尾的调用。
        </div>
      ) : (
        <ul className="unfinished">
          {calls.map((call) => (
            <li key={call.call_id} className="unfinished__item">
              <div className="unfinished__head">
                <span className="unfinished__name">{call.name}</span>
                <code className="unfinished__id">{call.call_id}</code>
              </div>
              <pre className="unfinished__args">{call.arguments}</pre>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
