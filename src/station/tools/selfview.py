"""全局工具 · 自我认知 + 产物（skills / artifacts）。

这两个不碰业务，只回答关于"这个工作站自己"的问题：
  · `skills`    —— 我会干啥？（用户问"你能做什么"、模型想确认某件事该谁干时）
  · `artifacts` —— 我产出过什么？（周报、4件套都能一键点开）

★ 09-13 删掉了原来的第三个工具 `show`（把仓库里的图/PDF 推到卡片上）。原因：
  它和 `ls`/`read`/`grep`/`write` 是**同一类**东西 —— "让 AI 看工作站自己"，
  而档案这种业务场景用不到它们（archive 有自己的 `show_page`/`show_overview`/
  `show_files` 推图，走 `/api/archive/page`，跟这里没关系）。删掉 `show` 之后，
  只为它存在的 `tools/guard.py` 路径闸和 `/api/station/file` 只读端点也一并清掉了
  —— 少一条"能读仓库任意文件"的对外入口。
"""
from __future__ import annotations

from station.core.tool import Tool


def t_skills(ctx) -> str:
    """列出这个工作站挂了哪些技能、各自能干什么。"""
    # 函数内 import：unified 会 import registry，registry 又要 import skills —— 顶层
    # 互相 import 容易绕成环，放进函数里就永远不会（Python 的循环导入老问题）。
    from station.app.unified import build_skill_catalog
    cats = build_skill_catalog()
    if not cats:
        return "当前没有挂载任何技能。"
    lines = ["这个工作站挂载了这些技能（用户提出相关需求时，路由会自动切过去）："]
    for c in cats:
        lines.append(f"- **{c.name}**（{c.id}）：{c.description}")
        if c.hints:
            lines.append(f"  常用说法：{c.hints}")
    lines.append("")
    lines.append("此外我随时可以：记长期记忆（remember/recall）、列产物（artifacts）。")
    # ★★ 这一句是给"模型发现自己手上没有那个工具"时的出路 —— 按本仓规矩，
    #    "我做不到什么"要写进**返回值**里（模型每轮读的是返回值，不是岗位须知）。
    #    09-14 实测：一条视频线程被误路由进档案技能后，模型知道该用 video.ask_photo
    #    （它自己记的笔记里写着），但手上没有，于是**一整轮 14 次调用全是
    #    remember/recall/skills**，绕圈找不到出路，用户干等什么都拿不到。
    lines.append("")
    lines.append("★ **你不能自己切换技能** —— 你手上只有当前这个技能的工具。")
    lines.append("  用户要的事如果**不在这份清单里你能调用的工具内**（比如你在档案技能里、"
                 "他却想做个视频），**直接说清楚，并建议他换个说法**"
                 "（例如「你换种说法，说\"我要做视频\"，路由会自动切过去」），然后就停下等他说。")
    lines.append("  切勿反复调 remember / recall / skills 去找出路 —— 它们**不会改变你手上的工具**，"
                 "只会把这一轮耗光，用户什么都拿不到。")
    return "\n".join(lines)


def t_artifacts(ctx, limit: int = 20) -> str:
    """列出"我的产物"（文件区里属于当前用户的文件）。"""
    from station.files import store as fs
    mine = list(fs.iter_files(getattr(ctx, "user_id", "") or ""))
    mine.sort(key=lambda d: d.get("created", 0), reverse=True)      # 新的在前
    if not mine:
        return "还没有产物。档案出件、周报导出之后会出现在这里。"
    n = max(1, min(int(limit or 20), 50))
    files = [{"name": d.get("name") or d["id"],
              "url": f"/api/files/{d['id']}?dl=1"} for d in mine[:n]]
    # ★ 把"我只能做到这一步"写进**返回值**，而不只是工具描述里 —— 返回值模型每轮都读得到，
    #   描述只是"事先的说明"。09-13 实测教训：模型会顺着"有 47 个产物"主动提出
    #   "要不要我按周报/4件套分类过滤一下"，而它根本没有这个能力 —— 这里把话说死，
    #   并把用户领到真正能按分组翻的地方（界面右上角的产物抽屉）。
    more = ""
    if len(mine) > len(files):
        more = (f"，这里只列了**最近 {len(files)} 个**；本工具**不能按类型过滤**，"
                f"要按产出分组翻全部，让用户打开界面右上角的「我的产物」抽屉")
    return {"text": f"共 {len(mine)} 个产物{more}。",
            # render 契约见 core/agent.run_tool：带 render 的 dict 会被拆成
            # "给模型的文字 + 给前端的卡片"。file-card 是前端已有的渲染器。
            "render": {"type": "file-card", "files": files,
                       "note": f"我的产物（{len(mine)} 个）"}}


def tools() -> list[Tool]:
    """本组工具清单。"""
    return [
        Tool(name="skills", label="看看会干啥",
             description="列出这个工作站挂载的所有技能和它们能做的事。用户问'你能干什么/有没有XX功能'时用它，别凭空猜。",
             run=t_skills, args=[]),

        Tool(name="artifacts", label="看我的产物",
             description="列出用户**最近**产出的文件（按时间倒序，默认最近 20 个），"
                         "显示成可点开的文件卡片。"
                         "用户问'我刚才导出的东西在哪/给我看看产物'时用它。"
                         "★ 它**只能按时间列最近 N 个，不能按类型或分组过滤** —— "
                         "没有'只列周报''只列档案 4 件套'这种能力，别答应这类请求。"
                         "用户想按产出分组翻找时，让他打开界面右上角的「我的产物」抽屉"
                         "（那里本来就是按一次产出分好组的）。",
             run=t_artifacts,
             args=[{"name": "limit", "type": "int", "desc": "最多列几个（按时间倒序取最近的），默认 20", "required": False}]),
    ]
