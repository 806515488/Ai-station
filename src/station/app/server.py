"""station Web 应用装配与启动。

接口按功能组放在 app/routes/；完整会话编排在 conversation/service.py。
本文件只创建应用、注册路由、挂载静态页面、管理生命周期与启动参数。
启动：python -m station.app.server，或已安装环境中的 station-web。
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# 兼容直接运行源码；包安装或 python -m 启动时也不重复加入搜索路径。
_HERE = Path(__file__).resolve()
for _path in (_HERE.parents[2], _HERE.parents[3]):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from station import config
from station.app.routes import ROUTERS


@asynccontextmanager
async def _lifespan(_app):
    from station.tasks.publisher import start_publisher, stop_publisher
    publisher = await start_publisher()
    try:
        yield
    finally:
        await stop_publisher(publisher)
        from station.search import client, service
        import asyncio
        await asyncio.to_thread(client.close)
        service.clear_cache()


app = FastAPI(title="station · 个人 AI 工作站", lifespan=_lifespan)

# 各功能组使用同一种注册方式；API 必须先于根路径的静态页面挂载。
for router in ROUTERS:
    app.include_router(router)

_STATIC = Path(__file__).parent / "static"
if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


def _serve_options() -> dict:
    """把环境变量翻成 uvicorn.run 的参数。

    单独拎成一个函数是为了**能离线测**：起真服务没法在单测里测，但"环境变量怎么翻"
    可以（见 tests/app/test_deploy.py）。

    默认值与本机开发**完全一致**（127.0.0.1:8001、不开 TLS）—— 本机用法一个字没变，
    服务器那套（docker-compose.yml）才靠这几个变量把它抬成对外服务：
      STATION_HOST            默认 127.0.0.1
      STATION_PORT            默认 8001
      STATION_SSL_KEYFILE / STATION_SSL_CERTFILE   两个都给了才开 HTTPS（自签也走这里）
    """
    opts = {"host": os.environ.get("STATION_HOST", "127.0.0.1"),
            "port": int(os.environ.get("STATION_PORT", "8001"))}
    keyfile = os.environ.get("STATION_SSL_KEYFILE", "")
    certfile = os.environ.get("STATION_SSL_CERTFILE", "")
    if keyfile and certfile:
        opts.update(ssl_keyfile=keyfile, ssl_certfile=certfile)
    elif keyfile or certfile:
        # 只给一个 = 配错了。**不当场崩**（崩了就完全没服务），以 HTTP 起站并把话说出来；
        # 静默降级才是危险的 —— 用户会以为自己在用加密连接。
        missing = "STATION_SSL_CERTFILE" if keyfile else "STATION_SSL_KEYFILE"
        print(f"[warn] 缺 {missing}，证书没配全 —— 这次以 **HTTP** 起站，连接没有加密。")
    return opts


def main():
    """命令行入口：`python -m station.app.server` 或 `station-web` 都跑到这。

    uvicorn = Python 版"Tomcat"。地址/端口/TLS 见 _serve_options()。
    ★ load_env() 放在**这里**而不是 __main__ 块里：`station-web` 这个入口（Docker 用的
      就是它）不会走 __main__ 块，放那儿等于漏读 .env。
    """
    import uvicorn
    config.load_env()          # 先读 .env 把 key 放进环境变量；没有 .env 就什么都不做
    uvicorn.run("station.app.server:app", reload=False, **_serve_options())


if __name__ == "__main__":
    main()
