# video（AI 视频生成）

按一句话描述生成一条 4–12 秒的短视频，走 Agnes AI 的异步视频接口。

- 类型：`agent`（对话面 + 后台执行器，一个模块两个入口）
- 入口：`skills/video/code/video_tools.py`
  - `tools()` → `video.draft` / `video.make` / `video.status` / `video.show`
  - `run_step(ctx, phase, **args)` → submit / poll / collect，分别保存阶段后释放 Worker
- 依赖槽位：**两个** —— 「通用模型」（把话补成完整画面）+「视频生成模型」（真生成）。
  后者默认是内置的 Agnes AI，密钥走 `AGNES_API_KEY`（或界面里单独填）。
- `manifest.heavy: false`：这是**纯网络等待型**长活（几分钟里本地几乎不吃内存），
  进入 remote 队列，不占档案识别/出件的 heavy Worker。
  吃内存的技能保持默认 heavy=true；队列总并发还取决于部署了多少 Worker。
- 口径外置：`prompts/optimize.md`（改它 = 改"怎么把一句话补成画面"，不用碰代码）

可试的话术：

- “生成一只猫跳上窗台的视频，5 秒”
- “做个竖屏的，咖啡在杯子里打转”
- “刚才那条好了吗？” / “放出来看看”

产物落在 `data/station/files/`，凭 `/api/files/<id>` 播放、`?dl=1` 下载。
