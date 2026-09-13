"""station.app —— 单用户 Web 宿主界面。

新手视角：app = “宿主对外的门面”（Controller + 前端静态页）。
  server.py = 所有 /api 接口（路由/请求/响应，≈Spring Controller）
  static/    = 浏览器端页面（原生 JS，无构建）
它只做转发与拼装，不写业务逻辑 —— 业务在 skills/，脑子在 core/agent.py。
"""
