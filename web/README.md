# web

前端界面。React + TypeScript + Vite。后端跑起来之后它由 FastAPI 挂在 `/` 上。

![界面](../docs/screenshot.png)

## 跑

```bash
# 后端（另开一个终端）
pip install -e .
python -m agent_runtime serve          # 需要 .env 里配好模型

# 前端
cd web
npm install
npm run dev                            # http://localhost:5173，接口代理到 8000
```

开发时用 `npm run dev`（有热更新，`vite.config.ts` 把 `/sessions` 和 `/healthz`
代理到后端）。要出单进程可跑的版本就 build 一次：

```bash
npm run build                          # 产物在 web/dist
```

之后后端起在 8000，直接访问 `http://127.0.0.1:8000/` 就能用 —— 后端会托管
`web/dist`。找不到 dist 时后端照常工作，只是 `/` 返回 404，不会起不来。

```bash
npm run typecheck   # tsc --noEmit
npm run lint        # eslint
npm test            # vitest
```

## 界面上的三块

- **左：会话列表。** 数据来自 `GET /sessions`，直接扫磁盘。所以刷新页面、
  甚至后端重启，之前的对话都还在 —— 会话状态本来就只存在于事件日志里，
  进程里没有任何东西需要恢复。
- **中：对话。** 用户消息、模型回复、工具调用卡片按发生顺序排开。
  工具卡片有三种状态：执行中 / 成功 / 失败，左边框颜色跟着变。
- **右：未收尾的调用。** 见下。

## 为什么值得看一眼

### SSE 是手解的，不是 `EventSource`

`/sessions/{id}/messages` 是 POST（消息在请求体里），而 `EventSource` 只能发 GET
且不能带 body。把它改成 GET 得把用户输入塞进 query string —— 会撞上 URL 长度限制，
还会把用户输入留在访问日志里。

所以走 `fetch` + `ReadableStream` 自己分帧（`src/api.ts`）。代价是必须处理
**一条帧被拆成多块到达**：按空行切，切出来的残片留在 buffer 里等下一块。
切早了 JSON 被截断，清空了事件就丢了。这段逻辑有单独的用例，包括
「把一条帧拆成两块再拼起来，结果和一次给全一样」。

### 状态是投影出来的，不是攒出来的

`src/entries.ts` 把事件投影成界面状态，两条路共用它：

- 实时收 SSE 事件（`applyEvent`）
- 刷新页面后读历史消息（`fromHistory`）

共用是刻意的：分开写的话，同一段对话在流式时和刷新后会长得不一样，
而这种 bug 只在「跑一半刷新」时才出现，最容易漏测。后端也是同一个思路 ——
runner 每一轮都从日志重放，所以「正常跑」和「崩溃后恢复」走同一条代码路径。

一个具体的约定：**工具调用之后的文本要开新气泡**。后端每个模型往返记一条
`assistant/message`，界面若把所有文本攒进同一个气泡，刷新前后分段就会对不上。

### 未收尾的调用

这是这个界面里别处看不到的东西，也是后端最值得一提的设计的直接体现。

工具调用在执行**之前**就先写进日志（`tool/call`），所以进程死在执行中途时，
日志里会留下一条没有 `tool/result` 的记录。重放时它被识别成
**「已受理、结果未知」**，而不是当它没发生过。

别的对话界面在这种时候只能显示「出错了」。这里能指出**具体哪一次调用**结果不明：

- 那张工具卡片会一直停在「执行中」，并写明「执行中途可能已经产生副作用，
  重试前请先确认」；
- 右侧面板列出它，附上调用参数。

运行时自己**不重试** —— 重试一个可能有副作用的写操作需要业务语义，
不该由运行时替用户拿主意。界面的职责是把状态说清楚，让人来定。

### 「耗时未知」

从日志回放出来的工具卡片显示「耗时未知」，而不是 0。因为 `tool/result`
事件里根本没记耗时字段，只有实时执行的那一次才知道 —— 显示成 0
是编数据，看起来还很正常。

## 演示数据

`docs/screenshot.png` 里的数据由 `tools/demo/seed_demo.py` 造出来，可复现：

```bash
python tools/smoke/fake_openai_server.py 8799 &
AGENT_PROVIDER=openai AGENT_MODEL=fake-model AGENT_API_KEY=x \
  AGENT_BASE_URL=http://127.0.0.1:8799/v1 AGENT_DATA_DIR=.data/demo \
  python -m uvicorn agent_runtime.api.app:app_from_env --factory --port 8000 &

python tools/demo/seed_demo.py --data-dir .data/demo
```

脚本跑一轮真实对话（走 HTTP + SSE），再往日志尾部追加一次「执行到一半就没了」
的工具调用。后者没法通过 API 造出来 —— 要制造崩溃得让进程正好死在工具执行中途，
而 API 就是那个进程。
