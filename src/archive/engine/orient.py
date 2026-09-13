"""翻拍件"该往哪转"的自动探测 —— 几何判"是不是横躺"，模型判"往哪边转"。

★ 09-12 修过一次准确性（用户报"有些图片方向还是反的"）：旧提示词只问"哪张文字是正立的"，
  实测 8 页标准答案上只对 6 页（两次错答都是把该 270° 的判成 90°，即整页倒过来）。
  病根是"让人想象转过去是什么样"这件事对视觉模型太难。改成问一个**具体的、看得见的位置**：
  "两张图里，大号标题字出现在页面的上边缘还是下边缘？" —— 同一批 8 页连测三次全对。
  另外把人工结果与自动结果分开标记（rotate_src），人工定的永不被自动覆盖，
  而提示词改好后自动结果能重新探（否则会被第一次的错误答案永久卡住）。

新手视角（Java 朋友版）：这是识别链的**收尾校正**步骤，不是 LangGraph 节点 ——
它在五个节点跑完之后、落库之前做一件事：把"横躺"的照片统一转向，好让后面
看页图、导出 PDF 时人一打开就是正的。

为什么需要它（全部是实测结论，见 docs/conventions.md 坑区）：
  - 翻拍设备**常常不写 EXIF 方向**：实测某卷 126 张里 113 张是"像素横躺"。
  - 几何只能判出"需要 ±90°"：**判不出往左还是往右**（横躺的纸，转 90 和转 270
    都能变竖版，差别是字正着还是倒着）。
  - 直接问视觉模型"这张图正不正"**不可靠**：实测把明显横躺的图判成"正立"。
  - 所以这里换问法：把**两个候选方向**摆在一起问"哪张的文字是正立的" ——
    判断题变成**二选一**。而且一卷通常是同一次翻拍、方向一致，所以**只探 1~2 张、
    整卷沿用**（每卷 2~4 次调用，不是每页）。

判不准（模型说 uncertain / 置信低 / 调用失败 / 样本之间打架）→ 返回 None，
调用方**一个字都不写**，退回"在收尾话里提一句、让用户自己说一声"那条路。
宁可不做，不可做错：写错了用户看到的是倒着的字，还得再来一次整卷操作。
"""
from __future__ import annotations

import base64
import json
import re

# 探测用的缩略图宽度：够看清"文字是不是正的"，又便宜（翻拍图动辄 4000px 宽）
_PROBE_W = 700


def is_landscape(path: str) -> bool | None:
    """这张照片"按 EXIF 摆正后仍是横版"吗？—— 是 = 多半该转 ±90。

    档案纸是竖的，横版多半是拍摄方向问题。读不动图返回 None（调用方当"不确定"跳过）。
    ★ EXIF 274（Orientation）在 5/6/7/8 时，摆正会宽高互换 —— 必须先换再比，
      否则"手机竖拍带 EXIF"的页会被误判成横躺。
    """
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.size
        if im.getexif().get(274) in (5, 6, 7, 8):
            w, h = h, w
        return w > h
    except Exception:                            # noqa：读不动就说不确定
        return None


def undecided_landscape(records: list) -> list:
    """"横躺、而且还没被人工定过方向"的页 —— 探测与写回都只碰这一批。

    ★ 判据是 `rotate_src != "manual"`（而不是"有没有 rotate"）：
      - `rotate_src="manual"` = 用户在对话里说过"这页转 90 度"（set_orientation 写的），
        **人比模型可靠，重跑识别时一个字都不能动**；
      - `rotate_src="auto"` = 上一轮自动探测写的 —— 提示词改好后**要能重新探**，
        所以它不算"定过"（新的探测结果会覆盖它）；
      - 没有这个字段（老卷、老版本写的）= 当作可探（历史值多半也是自动写的）。
      所以别把它写成 `not r.get("rotate")` —— 那样自动结果会永远卡在第一次的答案上，
      提示词改好了也白改。
    """
    return [r for r in records
            if is_landscape(r.get("path") or "")
            and r.get("rotate_src") != "manual"]


def _extract_json(text: str) -> dict:
    """从模型回复里抠出第一个 JSON 对象（模型爱加解释/代码围栏，见 graph._extract_json 同款）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:                            # noqa
        return {}


def _two_views(path: str) -> tuple[bytes, bytes] | None:
    """同一页的两个候选：逆时针 90° 与 顺时针 90°（=逆时针 270°）的缩略图字节。

    直接在缩略图阶段就转（imaging.thumb_bytes 的 rotate 参数）：不用把 4000px 的原图
    完整解码两遍，也不用手动 base64 大图。
    """
    from archive import imaging
    try:
        return (imaging.thumb_bytes(path, _PROBE_W, 90),
                imaging.thumb_bytes(path, _PROBE_W, 270))
    except Exception:                            # noqa
        return None


def probe_one(path: str, llm) -> tuple[int | None, str, str]:
    """对**一页**做二选一，返回 (角度|None, 置信, 一句说明)。

    单独拎出来是为了能"逐页"看判断结果 —— 量准确率时要知道模型对每一页怎么答的
    （整卷结论一致不代表每页都对），也方便单测直接戳这一层。
    """
    from archive.skill import loader

    views = _two_views(path)
    if views is None:
        return None, "", "读不出这一页的图"
    a, b = views
    b64 = lambda x: base64.b64encode(x).decode()          # noqa: E731
    try:
        out = llm.invoke([{"role": "user", "content": [
            {"type": "text", "text": loader.orient()},
            {"type": "text", "text": "图A（逆时针转 90°）："},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64(a)}"}},
            {"type": "text", "text": "图B（顺时针转 90°）："},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64(b)}"}},
        ]}])
        got = _extract_json(str(getattr(out, "content", out)))
    except Exception as e:                       # noqa：探测失败不拖垮识别
        return None, "", f"探测调用失败（{type(e).__name__}）"
    pick, conf = got.get("pick"), str(got.get("conf") or "").lower()
    if pick not in ("A", "B"):
        return None, conf, f"模型没给明确答案（{pick or '空'}）"
    deg = 90 if pick == "A" else 270             # A=逆时针90，B=顺时针90=逆时针270
    return deg, conf, f"选了 {'A（逆时针90°）' if pick == 'A' else 'B（顺时针90°）'}"


def auto_rotate(records: list, llm, channel: str = "",
                workers: int = 6) -> tuple[int, int, str]:
    """**逐页**探方向，把角度写回每一张横躺页的 rotate 字段。

    返回 (转正了几页, 判不准几页, 一句说明)。

    ★ 为什么是逐页、而不是"探一张、整卷沿用"（09-12 实测推翻了那个假设）：
      散页档案翻拍时**有的页是倒着拍的** —— 实测李明卷 126 页里，第 13 页要顺时针
      90°、而 50/87 页要逆时针 90°，同卷方向并不一致。"整卷一个角度"会把正确答案
      当成噪声投掉（第 13 页就是这么被 2:1 投错的）。
      逐页探测的准确率有实测支撑：**人工复核 5/5 全对**，且 qwen 与 glm 两个模型
      在 12 页上一致 11 页（瞎猜的话一致率只有 50%）。
      代价：每个横躺页 1 次调用，但发的是 **2 张 700px 小图**，比 mark 那一次发全尺寸
      原图便宜约 30 倍；而且**结果进 OCR 缓存**（键 = 图片 md5 + 通道 + 提示词指纹），
      重跑/重定类时为 0 成本。并行 6 路，和 node_mark 一个套路。
    判不准的那几页**一个字都不写**，在说明里报数（用户看一眼就能说一句整卷转）。
    """
    from archive.skill import loader
    from archive.storage import ocr_cache

    pages = undecided_landscape(records)
    if not pages:
        return 0, 0, ""
    if llm is None:
        return 0, len(pages), _hint(len(pages))
    prompt = loader.orient()

    def _one(r):
        """一页：先查缓存（命中 = 0 成本），未命中才掏两次调用，命中完顺手存。"""
        md5 = r.get("md5") or ""
        if md5:
            hit = ocr_cache.lookup(md5, channel, prompt)
            if hit and hit.get("deg"):
                return r, int(hit["deg"]), True
        deg, conf, _why = probe_one(r["path"], llm)
        if deg is None or conf != "high":
            return r, None, False
        if md5:                                   # 没 md5 的怪记录不写缓存（安全优先）
            ocr_cache.store(md5, channel, prompt, {"deg": deg},
                            model=getattr(llm, "model_name", ""))
        return r, deg, False

    from concurrent.futures import ThreadPoolExecutor, as_completed
    hit = miss = cached = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_one, r) for r in pages]):
            r, deg, was_cached = fut.result()
            if was_cached:
                cached += 1
            if deg is None:
                miss += 1                              # 判不准的页：不写，留给用户说
                continue
            r["rotate"] = deg
            r["rotate_src"] = "auto"                   # 标记来源：人工定的永不被覆盖
            hit += 1

    if not hit:
        return 0, miss, _hint(miss)
    msg = f"已自动转正 {hit} 页横躺照片（逐页判的方向"
    msg += f"，其中 {cached} 页命中缓存）" if cached else "）"
    if miss:
        msg += f"；另有 {miss} 页判不准，你看一眼说一声我单独转。"
    return hit, miss, msg


def _hint(n: int) -> str:
    """判不准时给用户的提示（不动数据，只告诉他怎么办）。"""
    return (f"另有 {n} 页是横躺的（自动判方向没把握）——"
            f"你看一眼页图，说一声\"逆时针转90度\"我就转正。" if n else "")
