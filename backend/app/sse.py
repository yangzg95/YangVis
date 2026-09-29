"""SSE 响应的公共部分：事件帧的格式与禁用缓冲的响应头。

对话回答、工作流运行进度这些流式端点都要用到同一套。放在这里而不是各路由自
带一份：``X-Accel-Buffering: no`` 漏写一处，表现就是那条流在 Nginx 后面整段
攒完才吐出来——本地开发看不出问题。
"""
from __future__ import annotations

import json


def sse_event(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which holds the whole
    # stream until completion and defeats the point of streaming.
    "X-Accel-Buffering": "no",
}
