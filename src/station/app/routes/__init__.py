"""HTTP 路由目录：按功能分组，在 server.py 统一注册，始终先于静态页面。"""
from . import (
    auth, chat, models, mcp, uploads, documents, sessions, traces, tasks, archive, files,
)

ROUTERS = (
    auth.router,
    chat.router,
    models.router,
    mcp.router,
    uploads.router,
    documents.router,
    sessions.router,
    traces.router,
    tasks.router,
    archive.router,
    files.router,
)
