"""一个最小的 OpenAI 兼容 SSE 服务，只为端到端冒烟测试用。

不依赖任何第三方库。**无状态**：看请求里有没有工具结果来决定回什么——
有工具结果就收尾，没有就先发起一次工具调用。用全局计数器的话，
反复跑冒烟会拿到上一次留下的状态。

用途是验证真实 HTTP 进出这条链路是通的。
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count

REQUESTS = count(1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        already_used_tool = any(m.get("role") == "tool" for m in messages)

        # 回显请求摘要，方便确认客户端确实把 system / tools / 历史发过来了
        summary = {
            "n": next(REQUESTS),
            "messages": len(messages),
            "tools": [t["function"]["name"] for t in (body.get("tools") or [])],
            "has_system": bool(messages and messages[0]["role"] == "system"),
            "already_used_tool": already_used_tool,
        }
        sys.stderr.write(f"[fake-llm] {json.dumps(summary, ensure_ascii=False)}\n")

        if not already_used_tool:
            chunks = [
                {"choices": [{"index": 0, "delta": {"content": "让我看下时间。"}, "finish_reason": None}]},
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "now",
                                            "arguments": '{"utc_offset_hours":8}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            chunks = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "现在是东八区时间，工具刚查到的。"},
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        for chunk in chunks:
            self._write(f"data: {json.dumps(chunk)}\n\n")
        self._write("data: [DONE]\n\n")
        self._write("")

    def _write(self, text: str) -> None:
        payload = text.encode("utf-8")
        if payload:
            self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
            self.wfile.write(payload)
            self.wfile.write(b"\r\n")
        else:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *_: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8731
    sys.stderr.write(f"[fake-llm] listening on 127.0.0.1:{port}\n")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
