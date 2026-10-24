"""把同步 Event 流接到异步 HTTP：只处理 SSE 编码、线程与关闭，不处理业务。

每次 next/close 在工作线程执行；isolated_stream 为整条流保留私有 Context，
使跨线程的追踪 span 能正确结束。断连后显式关闭生成器，不重放工具或外部操作。
"""
from collections.abc import Iterator

import anyio
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from station import observability as obs
from station.core.events import Event


def event_response(events: Iterator[Event]) -> StreamingResponse:
    """把内部 Event 迭代器变成浏览器可持续接收的 SSE 响应。"""
    async def stream():
        iterator = obs.isolated_stream(events)
        # 用独立对象区分“迭代结束”和普通返回值，避免 StopIteration 穿过异步边界。
        sentinel = object()
        try:
            while True:
                # 每次只取一项；业务停在检查点时，会先保存成功再产出下一条展示事件。
                event = await run_in_threadpool(next, iterator, sentinel)
                if event is sentinel:
                    return
                yield event.to_sse()  # 到这里才编码成 event/data 文本，实际发给浏览器。
        finally:
            # 客户端断开会取消当前协程；屏蔽取消，确保同步生成器的 finally 真正执行。
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(iterator.close)
    return StreamingResponse(stream(), media_type="text/event-stream")
