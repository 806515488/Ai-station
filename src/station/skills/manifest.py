"""Skill 数据模型与 manifest 读取。

manifest 用 JSON（stdlib 零依赖，便于开源/自部署；结构刻意与 Claude Skills/插件对齐，
将来可加一个 SKILL.md frontmatter→json 的转换或直接支持 yaml）。

文件：<repo>/skills/<id>/manifest.json
字段：id, ns(工具命名空间，默认=id), name, description, type(agent|pipeline|tools),
      entry(python import，如 station.demo_agent), keys(需要的 env key 白名单),
      model(text|vision), heavy(可选，默认 true，见下), icon(可选), builtin(可选，预留),
      args(可选，pipeline 参数表单描述，有序数组), args_one_of(可选，"至少填一"组)
args 数组每项：{name(=build_runner 形参名), label(表单标签),
              type(text|int|choice|upload),   # upload=客户端选照片上传→服务器目录
              required(默认 false), default, description, placeholder, min/max(仅 int),
              options(仅 choice)}；宿主按它在 Web 面板渲染成"人能看懂的表单" + 服务端 400 校验。

新手视角（Java 朋友版）：
  - manifest.json ≈ “技能安装包里的 application.yml / 描述文件”，是给机器读的身份证+说明书。
  - 照着现有技能的 manifest.json 抄一份，改 id/name/ns/type/entry，重启就多一个能力
    —— 宿主零改动，这就是“插件化”。
  - 本文件的 Skill 类 ≈ Java 的 DTO（把 json 读成对象）；三个 type 的区别看 docs/工作站架构设计.md §2。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field     # 见 tool.py：dataclass≈自动写构造器的“数据类”
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Skill:
    """一个技能的运行时对象：manifest 里的信息 + 装载后的可调用物。

    注释里 “(不进 manifest)” 的字段是宿主装完才有的，不来自 json。
    """
    id: str                 # 唯一 id（同时是目录名），如 archive
    ns: str                 # 工具命名空间前缀，如 weekly → 工具叫 weekly.xxx
    name: str               # 给人看的名字
    description: str        # 一句话说明（前端列表 & 模型判断“什么时候用这个技能”都靠它）
    type: str = "agent"     # agent | pipeline | tools（见设计稿 §2）
    entry: str = ""         # 代码入口，如 station.demo_agent / weekly_report
    keys: list[str] = field(default_factory=list)   # 需要的 env key 白名单（权限）
    model: str = "text"     # 默认用 text 通道还是 vision
    # ★ 这条技能的后台 job **会不会吃内存**（决定它要不要领 core/heavy.py 的"重活闸"）。
    #   默认 True = 受闸保护：全站同时只跑一件，防小内存机器被内核 OOM 杀掉。
    #   只有**纯网络等待型**长活才该写 false（如 skills/video：几分钟里几乎不吃本地内存，
    #   峰值就是最后下载的那几 MB 视频；让它占着闸会把档案识别/导出堵死几分钟）。
    #   ⚠️ **误用后果没有任何症状**：给一个真吃内存的技能标成 false，等于把 OOM 那道闸
    #   亲手拆掉 —— 不报错、不崩日志，只在某个大卷上和别的活撞上时被内核杀掉。
    #   拿不准就**别写这一行**（默认就是护着你的）。
    heavy: bool = True
    icon: str = ""
    group: str = ""         # 分组名（可选）：前端把同组技能叠成一张卡（如"干部档案"=整理+核对）
    builtin: list[str] = field(default_factory=list)
    # 可选：agent 型技能的"系统提示词文件"（技能目录下的相对路径，如 system.md）。
    # 装载时读进内存；server 对话时拼在消息最前——这是技能给模型的"岗位须知"。
    system: str = ""
    # 可选（不进 manifest 的老字段在注释下方运行时段）：pipeline 技能"参数表单"声明。
    # 注意：以下两个新字段必须带默认值 —— registry._load 用关键字构造 Skill，缺省会崩。
    args: list = field(default_factory=list)       # 可选：pipeline 参数表单描述（有序数组，见 validate_args）
    args_one_of: list = field(default_factory=list)  # 可选：[["photos_dir","project"],...] 每组"至少给一个"
    # —— 运行时装载（不进 manifest）——
    # ★ 这两个由 registry 按“entry 模块暴露了什么”来挂，**互不排斥**：技能可以只有
    #   tools（纯对话能力 / 叶子能力）、只有 build_runner（纯后台执行器）、或两者都有
    #   （skills/archive：工具面 + 耗时的识别执行器）。type 只决定它对用户的形态。
    tools: list = field(default_factory=list)      # 技能自带的工具（模块写了 tools() 才有）
    build_runner: Optional[Callable] = None        # 任务生成器入口（模块写了 build_runner() 才有）

    # to_info() 已删（09-11）：唯一用途是喂 /api/capabilities 渲染前端技能栏，
    # 技能栏随"统一主对话"一起删了。icon/group/args 这几个 manifest 字段因此
    # 不再有任何消费方 —— 解析仍保留（registry 会读），别再往界面描点子上想。


def load_manifest(folder: Path) -> dict:
    """读 <技能目录>/manifest.json → dict；没有该文件返回空 dict。

    Path 可以 f"/a/b"; folder/"manifest.json" 直接拼子路径，跨平台安全。
    """
    p = folder / "manifest.json"
    if not p.is_file():
        return {}
    # read_text 读字符串 → json.loads 解析成 Python dict（≈Jackson readValue）
    return json.loads(p.read_text(encoding="utf-8"))


def validate_args(fields: list[dict], args: dict,
                  args_one_of: list[list[str]] | None = None) -> str | None:
    """对照 manifest 的 args 声明，校验一次 job 参数提交。

    纯函数（本文件只依赖 stdlib），方便离线单测与复用。
      fields       技能的 args 字段描述符列表（manifest 的 "args"）
      args         本次提交的参数 dict（POST /api/jobs 的 body.args）
      args_one_of  "至少给一个"的组列表，如 [["photos_dir","project"]]
    返回 None = 通过；否则返回中文错误文案（给 HTTP 400 用）。
    顺序：未知键 → 缺必填 → 值类型/区间 → args_one_of 组。fields 空 = 老技能没声明 → 不校验。
    """
    if not fields:
        return None                                  # 无 args 声明的技能向后兼容，放行
    allowed = {f["name"] for f in fields}
    by_name = {f["name"]: f for f in fields}

    # ① 未知参数：根治"把 demo 的 steps 发给 archive"那类错 → 提前拦成 400
    unknown = sorted(set(args) - allowed)
    if unknown:
        return (f"参数 {'、'.join(unknown)} 不被该技能支持；"
                f"可用参数：{'、'.join(sorted(allowed))}")

    # ② 缺必填（消息用中文 label，比裸字段名友好；无 label 才退回 name）
    missing = [f.get("label") or f["name"] for f in fields
               if f.get("required") and f["name"] not in args]
    if missing:
        return "缺少必填参数：" + "、".join(missing)

    # ③ 逐个轻校验（只查这次真的出现了的参数）
    for name, val in args.items():
        f = by_name[name]
        label = f.get("label") or name
        if f.get("type") == "int":
            if isinstance(val, bool):                # bool 是 int 的子类，先排除
                return f"{label} 需为整数"
            try:
                iv = int(val)                        # 允许纯数字串 "5"
            except (TypeError, ValueError):
                return f"{label} 需为整数（收到：{val}）"
            lo, hi = f.get("min"), f.get("max")
            if lo is not None and iv < int(lo):
                return f"{label} 需 ≥ {lo}"
            if hi is not None and iv > int(hi):
                return f"{label} 需 ≤ {hi}"
        elif f.get("type") == "choice":
            opts = f.get("options") or []
            if val not in opts:                      # 挡住 order 乱填
                return f"{label} 只能取：{'、'.join(map(str, opts))}"

    # ④ "至少给一个"的组（photos_dir / project 全缺才拦，单给其一放行）
    for g in (args_one_of or []):
        if g and not any(k in args for k in g):
            return "需提供 " + " 或 ".join(g) + " 之一"
    return None
