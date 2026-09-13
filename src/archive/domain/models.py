"""领域实体类型：PageCard / Candidate / MaterialRow / Issue / ReviewState。

新手视角（Java 朋友版）：这是【DTO/数据模型区】——先约定好“一条记录/一个材料/一个问题”
长什么样（有哪些字段、什么类型），其它模块(engine/export/审核)照这个契约读写。
Python 里用 TypedDict 表达“字典的字段约定”（≈给 Map 定义 schema，便于类型提示/自文档化），
用 @dataclass 表达真正的“对象”。字段大多可选（total=False），因为识别过程是逐步填满的。
"""
from dataclasses import dataclass, field
from typing import Literal, Optional, TypedDict


class PageCard(TypedDict, total=False):
    """建档(mark)阶段给【每一页】打的内容卡字段约定。

    total=False：字段都可缺（识别中途/失败时可能没填全）。
      t         标题/表头（册内栏目页这里是【栏目名】，不是材料名）
      doc       文号
      date      制成时间 dict{y,m,d}
      s         正文要点摘要
      u         是否拿不准(uncertain)/doubt
      form      ★这页属于哪本册子/哪张表单的正式名称（如"干部履历表"）；
                判不出就是 None —— 归组靠它，宁可留空也不能猜
      pk        ★本页的栏目名（如"工作经历""入党介绍人的意见"）；
                独立单页件填与 t 相同
      pg        ★页面上【印刷的】页码/栏目序号；只认印上去的，不许推断
    """
    t: Optional[str]
    doc: Optional[str]
    date: Optional[dict]
    s: Optional[str]
    u: bool
    form: Optional[str]
    pk: Optional[str]
    pg: Optional[int]


class Candidate(TypedDict):
    """切份(segment)产出的“一份候选材料”结构。

    注意 seqs 与 raw_seqs 的分工（照片顺序随机后必须区分）：
      - seqs      按【材料内页序】排好的页 —— 装订/导出用这个顺序
      - raw_seqs  原始照片号升序 —— 人靠它在相册/页面上找实物
    """
    idx: int             # 这份候选的编号（按目录序排，1..n）
    seqs: list[int]      # 属于它的页 seq 列表（已按装订序排好）
    dup_groups: list[list[int]]   # 一式 N 份：其它完全相同份的页组
    copies: int          # 一式几份（1=一份，>1=有重复份）
    pages: int           # 这份实际张数
    form: Optional[str]  # 身份特征：册名/表单名（None = 没判出来，靠残页裁决）
    date: Optional[dict] # 桶的日期（单页件=该页日期；册子=共识日期或 None）
    rep: int             # 代表页 seq（取材料名/日期、定类时看它）——不一定是照片号最小的
    raw_seqs: list[int]  # 原始照片号升序（给前端"第N张"标签用）
    doubt: bool          # 这桶的归属是否存疑（需人工核对）
    orphan: bool         # 残页：连 form 都认不出（要交给 resolve 节点问模型）


class MaterialRow(TypedDict, total=False):
    """一条【正式材料】（归组→裁决→定类→build_mats 之后），也是导出目录的一行。"""
    seq: int             # 材料编号（类内从 1 起）
    uid: str             # 唯一 id（合并/引用用），如 m3
    category: Optional[str]   # 落到的小类，如 九-1（非法值=None → 未定类）
    main: Optional[str]       # 所属大类，如 九
    title: Optional[str]      # 材料名称
    title_src: str            # 名称来源标记（engine 推断/原文/unknown）
    date: Optional[dict]      # 制成时间 {y,m,d}
    copies: int               # 份数
    pages: int                # 页数
    members: list[int]        # 本份覆盖的页 seq（★按装订序，不是照片序）
    dup_pages: list[int]      # 一式 N 份里重复份的页
    assigned_pages: list[int] # 本行实际归属的全部页（members+dup）
    evidence: str             # 定类依据片段（给报告/审核看）
    doubt: bool               # 是否存疑（需人工核对）
    verdict: str              # 判定：ok / doubt 等
    raw_seqs: list[int]       # 原始照片号升序（找实物用）
    rep: int                  # 代表页 seq（材料名/日期取自它）
    form: Optional[str]       # 册名/表单名（归组身份特征）


class Issue(TypedDict):
    """一个“问题/待核对项”。"""
    level: Literal["error", "warn", "infer", "info"]   # 严重级别
    code: str              # 问题码（如 C0-未归类 / C1-归类存疑）——前端按码分类显示
    message: str           # 人话描述
    seq: list[int]         # 涉及哪些页（便于定位）


@dataclass
class ReviewState:
    """人工审核时的一整份“可编辑状态”（缓存整卷 records+materials+issues）。

    @dataclass 见 tool.py：自动生成构造器。它像一个“包住整份审核现场”的袋子，
    前端每次编辑 → 更新这里 → 保存 → 重新导出。
    """
    records: list = field(default_factory=list)      # 全部页记录（含识别结果）
    materials: list = field(default_factory=list)    # 材料行
    issues: list = field(default_factory=list)       # 待核对问题
    notes: list = field(default_factory=list)        # 去重/一式N份 说明文字（进报告）
