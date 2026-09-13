"""Registry —— 扫 <repo>/skills/*/manifest.json，装载 entry 模块暴露出来的东西。

entry 解析：import 模块后**看它暴露了什么就挂什么**（两个可以同时存在，互不排斥）：
  模块.tools()        → list[Tool]（name 为叶子，装载时补成 <ns>.<leaf>）
  模块.build_runner() → 生成器工厂，见 station/jobs/manager 契约
manifest 里的 type 只管“这个技能对用户是什么形态”（agent=对话入口 / pipeline=纯后台），
**不参与装载判断** —— 一个技能可以既有工具面、又带一个耗时执行器（如 skills/archive：
工具面 + 识别执行器同一个技能，不再拆两个目录）。
工具最终名统一带命名空间，实现“只有本技能白名单内工具对模型可见”。

新手视角（Java 朋友版）：
  Registry ≈ 一个“技能仓库/技能管理器”（SkillRepository），并且是【单例】——
  全进程只有一份，谁要查技能都来这。
  它做三件事：
    1) load_all / load    —— “加载”：扫描 skills/ 目录，读 manifest.json
    2) _load              —— “解析”：manifest 的 json → Skill 对象；再按 entry import 代码
    3) ids/get/list/...   —— “查询”：给 server/UI 用（list 出有哪些技能、给 /api/capabilities）
  加载失败的原则 = 【静默跳过】不崩：某个技能写坏了，宿主照常跑，只是不出现它。
"""
from __future__ import annotations

import importlib        # 动态 import 模块（importlib.import_module("x.y")≈Java Class.forName）
import sys              # 拿 sys.path：Python 找模块的“classpath”
from pathlib import Path

from station import config
from station.core.tool import Tool
from station.skills.manifest import Skill, load_manifest


def _load_tools(module, ns: str) -> list[Tool]:
    """问技能模块“你能提供哪些工具”，并把工具名补成带命名空间的完整名。

    getattr(模块, "tools", None)：安全取模块的 tools 属性，没有返回 None。
    callable(fn)：判断它是不是可调用的函数。技能模块约定提供 tools() 返回工具列表。
    """
    fn = getattr(module, "tools", None)      # 拿“工具清单生成函数”；模块没写 tools 就 None
    raw = fn() if callable(fn) else []       # 有就调用它拿原始工具列表，没有给空 []
    out: list[Tool] = []
    for t in raw:
        if not isinstance(t, Tool):          # 防御：清单里混进了非 Tool 的东西就跳过
            continue
        # 关键：把“now”补成“demo.now”。技能里写的是叶子名，宿主统一成 <ns>.<leaf>，
        # 这样不同技能里都叫 now 也不会撞名。
        t.name = f"{ns}.{t.leaf()}"
        t.owner = ns                         # 记下归属技能（权限/作用域用）
        out.append(t)
    return out


def _import_entry(entry: str, folder: Path | None = None):
    """解析 entry 'mod[:attr]'：把字符串“翻译”成一个可用的模块/函数。

    entry 例子：
      "station.demo_agent"            → import 这个模块
      "archive.station_adapter"       → import 既有业务包里的模块
      "weekly_report"                 → 技能自带代码：skills/<id>/code/ 下的模块

    新手视角：import 是 Python 找模块的机制，它只会去“sys.path 里的目录”找。
    技能自带 code/ 不在默认 path 上，所以 ModuleNotFound 时我们手动把
    skills/<id>/code 塞进 sys.path 再 import 一次 —— 这就是“宿主支持自带代码”的机关。
    """
    mod, _, attr = entry.partition(":")   # 拆成 模块名(:属性名)；没有冒号则 attr 为空
    try:
        m = importlib.import_module(mod)  # 第一次尝试：走普通 import 路径
    except ModuleNotFoundError:           # 没找到 → 可能是技能自带的本地代码
        if folder is not None:
            code_dir = folder / "code"    # skills/<id>/code/
            if code_dir.is_dir():
                sp = str(code_dir)
                # 防重复塞入同一条路径（同目录被扫两次时不报错）
                if sp not in sys.path:
                    sys.path.insert(0, sp)  # 把技能代码目录加进“模块搜索路径”
                m = importlib.import_module(mod)   # 再试一次就能找到了
            else:
                raise                     # 没 code 目录还是没找到 → 原样抛错
        else:
            raise
    # 若 entry 形如 "mod:attr"，返回模块里的那个属性（如函数）；否则返回模块本身
    return m if not attr else getattr(m, attr)


def _read_system(folder: Path, rel: str) -> str:
    """读技能的"系统提示词"文件（agent 型技能给模型的岗位须知）。

    manifest 里 "system": "system.md"（相对技能目录）；文件不存在给空串（不报错——
    没写系统提示词的技能照常工作）。为什么放文件而不是 manifest 字段：提示词常
    几百字还带换行，放 json 里没法读；放 md 里人能直接编辑、diff 也清楚。
    """
    if not rel:
        return ""
    p = folder / rel
    try:
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    except Exception:                                # noqa：读不动就当没有（静默降级）
        return ""


class Registry:
    """技能注册表：内存里存 {技能id -> Skill 对象} 的字典，并负责加载/查询。

    你可以把它想成 Java 里一个带 Map 的 Manager：字段 _skills 就是这个 Map。
    """

    def __init__(self, scan_dir: Path | None = None):
        """构造器：决定“去哪扫技能”。默认去仓库根 skills/ 目录。

        scan_dir or config.SKILLS_DIR：or 的意思是“前者为 None 就用后者”。
        Path | None 注解 = “这个参数可以是 Path，也可以是 None（可空）”。
        """
        self._dir = scan_dir or config.SKILLS_DIR     # 技能目录（默认 skills/）
        self._skills: dict[str, Skill] = {}           # 内存表：技能id -> Skill 对象

    # ── 装载 ──
    def load_all(self) -> "Registry":
        """开机全量扫：遍历技能目录下每个子目录，有 manifest.json 就装载成一个技能。

        返回 self（自己）：方便写成 Registry().load_all() 这种链式写法（类似 Builder）。
        "Registry" 引号返回类型：因为类型名在类里还不能直接用（靠 __future__ 才可不加引号）。
        """
        if not self._dir.is_dir():        # 防御：skills/ 目录不存在就直接返回
            return self
        # iterdir() 列出子目录；sorted 排序让加载顺序稳定（不依赖磁盘顺序）
        for folder in sorted(self._dir.iterdir()):
            m = load_manifest(folder)     # 读 <目录>/manifest.json → dict
            if not m.get("id"):           # 没有 id 字段 = 不是合法技能目录 → 跳过
                continue                  # （continue = 跳过本次循环继续下一个）
            self._skills[m["id"]] = self._load(m, folder)   # 解析并存进内存表
        return self

    def load(self, skill_id: str) -> Skill | None:
        """按 id 单独加载一个技能（一般用于运行时动态补装）。加载失败返回 None。"""
        folder = self._dir / skill_id     # 拼出它的目录：skills/<id>/
        m = load_manifest(folder)         # 读 manifest
        if not m.get("id"):
            return None                   # 没有这个技能 → None（调用方好判断）
        s = self._load(m, folder)
        self._skills[skill_id] = s        # 存进内存表（覆盖旧对象）
        return s

    def _load(self, m: dict, folder: Path) -> Skill:
        """把一个 manifest dict “翻译”成真正的 Skill 对象，并试着 import 它的代码。

        这段最值得看：manifest.json 的每个字段 → Skill 构造参数的对应关系。
        """
        # —— 第一步：把 json 字段填进 Skill（字段齐全的才用；缺省给默认值）——
        s = Skill(id=m["id"],                              # id 是必填，直接 m["id"]
                  ns=m.get("ns", m["id"]),                 # 命名空间，缺省用 id
                  name=m.get("name", m["id"]),
                  description=m.get("description", ""),
                  type=m.get("type", "agent"),             # 缺省默认 agent 型
                  entry=m.get("entry", ""),                # 代码入口（import 谁）
                  keys=m.get("keys", []),                  # 需要的 key 白名单
                  model=m.get("model", "text"),
                  icon=m.get("icon", ""), builtin=m.get("builtin", []),
                  group=m.get("group", ""),                # 前端分组（可选，如"干部档案"）
                  # 系统提示词：读技能目录下的 system.md（agent 型的"岗位须知"，见 _read_system）
                  system=_read_system(folder, m.get("system", "")),
                  # 可选：pipeline 参数表单声明（新键，or [] 归一 null → 空数组）
                  args=m.get("args") or [],
                  args_one_of=m.get("args_one_of") or [])
        # —— 第二步：按 entry import 技能代码 ——
        try:
            obj = _import_entry(s.entry, folder)   # entry 可能是 station.* / archive.* / 本地 code
        except Exception:                       # noqa
            obj = None                           # 加载失败就当“没代码”，不崩宿主
        # —— 第三步：模块暴露什么就挂什么（不再按 type 二选一）——
        # 两个判断互相独立：只写 tools() = 纯对话能力；只写 build_runner() = 纯后台执行器；
        # 两个都写 = 既有对话面、又带耗时执行器（skills/archive 就是这种）。
        if obj is not None:
            if callable(getattr(obj, "tools", None)):
                # 问模块“有什么工具”(tools()) → 存进 s.tools
                s.tools = _load_tools(obj, s.ns)
            if callable(getattr(obj, "build_runner", None)):
                # 问模块“长活怎么跑”(build_runner) → 存进 s.build_runner
                s.build_runner = obj.build_runner
        return s

    # ── 查询 ──
    # ids() / capabilities() 已删（09-11）：它们的唯一调用方是 /api/capabilities，
    # 那个端点随"统一主对话"一起没用了（前端不再画技能栏）。
    def get(self, skill_id: str) -> Skill | None:
        """按 id 取 Skill 对象；没有返回 None（而不是抛异常）。dict.get 天然不会越界。"""
        return self._skills.get(skill_id)

    def list(self) -> list[Skill]:
        """列出所有 Skill 对象（dict 的 value 列表）。"""
        return list(self._skills.values())


# ── 单例（进程内只允许一个注册表）────────────────────────────────
# 模块顶层变量 ≈ Java static 字段。注解 _registry: Registry | None 表示“现在是空的(单例还没建)”
_registry: Registry | None = None


def get_registry() -> Registry:
    """取全局唯一的 Registry；没有就现场建一个并全量扫描（懒加载单例）。

    global _registry：告诉 Python “我要给模块顶层的 _registry 赋值，别当局部变量”。
    原理（Java 对比）：Python 函数里出现 `x = ...` 就默认 x 是局部变量；
    要改模块级的同名变量，必须先用 global 声明 —— 等价于 Java 静态字段 `MyClass.registry`。
    如果没有这行，_registry = ... 只会新建一个局部变量，单例就永远“存不住”。
    """
    global _registry
    if _registry is None:                 # 还没有 → 建一个并加载全部技能
        _registry = Registry().load_all()
    return _registry
