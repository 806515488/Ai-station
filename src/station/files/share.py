"""能力令牌 —— 把某个文件**临时**开给外部服务抓取。

给谁用：视频生成要把人像照片交给 Agnes，而对端只接受"**可由 Agnes 服务公开访问的
URL**"。所以我们必须开一个**不登录也能取到、但不可猜、会过期、用完就撤**的链接。

为什么不能直接拿文件 id 当公开路径（`/api/files/<fid>` 那样）：
  · `fid` 是内容的 sha1 —— 公开它等于给了一个**可枚举的面**；
  · 它**没有过期概念** —— 一旦公开就是永久公开。用户传的是自己的照片，不能这样。

★ 这个模块是**全仓唯一的签发口**（和"同一份文件只能有一条写路径"同源）：
  令牌只能指向一个已存在的文件、不能带任何定位参数（路径/序号/fid）、必须有过期、
  必须在任务终态后撤销。要加新的"让外部抓我们的东西"的能力，走这里，别另开一条。

新手视角（Java 朋友版）：≈ 一个一次性、带过期的 S3 预签名 URL 生成器，
只不过我们把它存在自己的库里（token → 文件 id），取的时候查表。
"""
from __future__ import annotations

import os
import secrets
import time

from station import config, db

# 令牌默认有效期（秒）。
# ★★ 必须**覆盖整条任务**：官方要求"媒体链接在任务完成前保持有效"，而实测一条 5 秒的
#   片子端到端要 290 秒（大部分在对端排队），我们自己的轮询上限更是 900 秒。
#   给"5 分钟"那种看着安全的值，会在对端排队时**静默**把图撤掉 —— 症状是生成出来的
#   视频里没有这个人，而且**不报错**（最贵的那种错）。
DEFAULT_TTL = 3600.0


def link_ttl() -> float:
    """令牌有效期（秒），可用 STATION_REF_TTL 覆盖。

    ★ 用函数而不是模块级常量：`load_env()` 是 main() 里才调的，常量会静默读到默认值。
    """
    config.load_env()
    try:
        v = float(os.environ.get("STATION_REF_TTL") or DEFAULT_TTL)
    except (TypeError, ValueError):
        return DEFAULT_TTL
    return v if v > 0 else DEFAULT_TTL


def make_ref_link(fid: str, owner: str, ttl: float | None = None) -> str:
    """给一个文件签一张临时取件码，返回 **token**（URL 用 `ref_url()` 拼）。

    没配对外地址时抛 `ValueError`（消息是给人看的）。
    ★ **先校验再落库**：拒绝的时候表里不该留下半成品行。
    """
    if not (fid or "").strip():
        raise ValueError("没有要分享的文件")
    if not config.public_base():
        raise ValueError(config.public_base_hint())
    db.ref_link_sweep()                      # 顺手清过期的（不需要后台定时任务）
    token = secrets.token_urlsafe(32)        # 256 bit、URL 安全；**别换成 uuid**
    db.ref_link_add(token, fid, owner,
                    time.time() + (link_ttl() if ttl is None else float(ttl)))
    return token


def ref_url(token: str) -> str:
    """令牌 → 那个"对端能抓到的"绝对 URL。没配对外地址时返回空串。"""
    base = config.public_base()
    return f"{base}/api/ref/{token}" if (base and token) else ""


def revoke(tokens) -> int:
    """撤销若干令牌。返回真删掉几条。

    ★★ 这里**自己把异常吞掉**，是有意的：撤销是"善后"，不该有能力改变业务结果。
      调用方是在任务**已经成功生成之后**才来撤销的 —— 数据库抽一下就让一个
      "视频已经生成好了"的任务被判成失败，用户白等几分钟还看不到片子，那不可接受。
      把它封在这一层，是为了让调用方**没有机会写错**。
    """
    try:
        return db.ref_link_revoke(list(tokens or []))
    except Exception:                        # noqa：善后失败不该反噬业务
        return 0
