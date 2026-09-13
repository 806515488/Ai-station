"""demo-pipe —— pipeline 型示例技能：异步分段任务→产一个 txt 产物。

验证：jobs 队列 + 进度事件 + 产物落盘/预览。

新手视角：pipeline 型和 agent 型的差别 = “确定的流水线 vs 自由的聊天”。
  - agent 型：每一步怎么走由模型临场决定（适合开放任务）。
  - pipeline 型：步骤写死、像工厂流水线，只汇报进度、最后交产物（适合
    “一定能跑完、只是慢”的任务，如批量档案、生视频）。由 jobs 后台跑。

契约（与 jobs/manager.py 对齐）：build_runner 是【生成器函数】，宿主一个
for 循环喂着它跑；它每 yield 一次事件，manager 就更新一次任务状态：
    {"type":"progress", "percent":0-100, "message": str}   # 报进度
    {"type":"artifact", "id": 文件id}                       # 产出一个文件
  迭代结束 = 任务成功。
"""
from __future__ import annotations

import time


def build_runner(ctx, steps: int = 5, tag: str = "demo"):
    """一次 demo 流水线：跑 n 步，每步睡 0.2 秒装“很忙”，最后落一个 txt 产物。

    注意这个函数里到处是 yield —— 有 yield 就是生成器：它不是一口气跑完，
    而是“走一步停一下”，宿主每取一次值就往下走一点，途中能实时上报。
    """
    # 防御输入：把 steps 限在 [1,50]，防止传负数/超大步数把任务拖太久
    n = max(1, min(int(steps), 50))
    lines = [f"demo-pipe 产物 tag={tag}"]
    for i in range(n):
        # 假装在做一个很重的步骤（真实 pipeline 里这里可能是：识别/生成/合成…）
        time.sleep(0.2)
        lines.append(f"step {i + 1}/{n}")
        # 上报进度：percent 算成整数百分比，message 给一句人话
        yield {"type": "progress",
               "percent": int((i + 1) / n * 100),
               "message": f"第 {i + 1}/{n} 步"}

    # 全部步骤完成 → 把结果存进 station 文件区（见 files/store.py），拿回一个文件 id
    from station.files import store as fs       # 函数内 import：演示“用到才导”
    fid = fs.save_bytes(("\n".join(lines) + "\n").encode("utf-8"), ".txt",
                        name=f"demo-{tag}.txt",
                        owner=getattr(ctx, "user_id", "") or "",
                        skill_id=getattr(ctx, "skill_id", "") or "demo-pipe",
                        group_key=f"demo:{tag}",
                        group_label=f"Demo 流水线 · {tag}")
    # 上报产物：manager 会把它记进 job.artifacts，前端就能下载
    yield {"type": "artifact", "id": fid}
    # yield 完、函数走到结尾 = 任务成功（manager 把状态置 done）
