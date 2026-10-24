"""station.app —— 单用户 Web 宿主界面。

新手视角：app = “宿主对外的门面”（Controller + 前端静态页）。
  server.py = 创建应用、统一注册路由、静态页面和启动
  routes/   = 按功能分组的 HTTP 接口（≈Spring Controller）
  auth.py / streaming.py / probes.py = 共用登录态、SSE 适配和连接测试限流
  static/    = 浏览器端页面（原生 JS，无构建）
会话用例在 conversation/service.py，共用回答在 chat/service.py，技能业务在 skills/，模型/工具循环在 core/agent.py。
"""
