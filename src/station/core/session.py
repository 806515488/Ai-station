"""Thread —— 一次对话/任务 = 一个 Thread，内容条目化（messages 为纯 dict，可落盘）。

对标三家 harness 的 Thread/Turn/Item：这里先做到“消息条目 + 跨轮续聊 + 待批准挂起”，
更重的 context 压缩/回放在 core.compact 渐进补。

新手视角：Thread = 你和 AI 的那份“聊天记录本”，能存盘、能续聊。
  - msgs 里的每条消息就是一个 dict：{role: user|assistant|tool, content:…}。
    role=tool 的消息就是“工具执行结果”，模型靠它接着想。
  - 为什么每次对话要带整本历史？因为模型没记忆 —— 你每说一句，都要把“到目前为止
    聊了什么 + 工具结果”整包发给它，它才知道上下文。
  - pending：危险工具跨回合“请求→允许/拒绝”时，把待批准信息挂这里（见 server.py 的
    in-band 批准逻辑和 core/agent.py 的批准闸）。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path


def _now() -> float:
    return time.time()       # 当前 Unix 时间戳（秒），用来排序/记录时间


class Thread:
    """messages 为纯 dict 列表（role/user/assistant/tool + tool_call_id），直接可给模型。

    “纯 dict”很重要：存盘(json) / 发给模型 都不用转换，天生可序列化。
    09-06 起归属用户：user_id 由 server 在创建/读取时填上（谁开的会话算谁的）。
    """

    def __init__(self, skill_id: str = "", thread_id: str | None = None):
        # uuid4().hex[:16] 生成一个随机短 id 当会话号（不太可能撞）
        self.id = thread_id or uuid.uuid4().hex[:16]
        self.skill_id = skill_id
        self.msgs: list[dict] = []                     # 聊天/工具历史（核心）
        self.meta: dict = {"created": _now(), "summary": ""}   # 杂项；pending 等挂起态也放这
        self.updated = _now()
        self.created = self.updated                    # 建会话时间（DB 列用）
        self.user_id = ""                              # 归属用户（server 填）

    # ── 消息 ──
    def add(self, msg: dict):
        """往历史里追加一条消息。每追加一次就刷新 updated（前端排序用）。"""
        self.msgs.append(msg)
        self.updated = _now()

    def add_user(self, text: str):
        """快捷方法：加一条“用户说的话”。dict 结构见文件顶部新手视角。"""
        self.add({"role": "user", "content": text})

    def last(self) -> dict | None:
        """取最后一条；历史为空返回 None（而不是越界报错）。"""
        return self.msgs[-1] if self.msgs else None

    # ── 待批准挂起（in-band 批准：一回合“请求”，下一回合“允许/拒绝”后补执行）──
    @property
    def pending(self) -> dict | None:
        """只读属性：pending —— 语法上像取字段 t.pending，实际是调这个函数。"""
        return self.meta.get("pending")      # 没有就返回 None

    def set_pending(self, tool: str, arguments: dict, preview: str = ""):
        """把“正在等批准的 工具名+参数（+ 一句给用户看的预览）”存起来，跨回合记住。

        preview 由**工具自己**算（见 core/tool.Tool.preview）—— 宿主不认识业务，
        说不出"这一步到底要写什么"。存下来而不是每次重算：刷新页面后要重画同一张卡片，
        重算可能跟当时不一致。
        """
        self.meta["pending"] = {"tool": tool, "arguments": arguments,
                                "preview": preview, "at": _now()}

    def clear_pending(self):
        """用户回答完（允许或拒绝）就清掉，别让下次对话误触发。"""
        self.meta.pop("pending", None)       # .pop(k, None)：有就删，没有也不报错

    # ── 持久化（09-06 起走 SQLite，见 station/db.py；root 参数保留兼容旧测试）──
    def save(self, root: Path | None = None):
        """把整个 Thread 存进 SQLite（threads 表）。

        root 参数已废弃（历史签名兼容）：传了也不走文件——测试里要隔离数据库，
        用 conftest 把 station.db.DB_PATH/连接指到临时目录。
        """
        from station import db
        db.thread_save(self)

    @classmethod
    def load(cls, thread_id: str, root: Path | None = None) -> "Thread | None":
        """按 id 从 SQLite 读回一个 Thread；没有返回 None。

        @classmethod：以“类”调用 Thread.load(...)，第一个参数自动是类本身(cls)。
        """
        from station import db
        return db.thread_load(thread_id)


# list_threads() 已删（09-11）：零调用方。前端"历史会话"直接调 db.thread_list（带用户过滤）。
