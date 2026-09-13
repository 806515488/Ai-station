# demo-pipe

pipeline 型技能的冒烟样例：宿主把一次异步任务交给它，它分步吐进度，最后落一个文本产物。

- 类型：`pipeline`（固定流程、异步、走 jobs 队列）
- 入口：`station.demo_pipe`（`build_runner(ctx, **args)` 生成器，yield progress/artifact 事件）
- 前端点“运行”即 POST /api/jobs → 状态机 queued→running→done，产物可下载

pipeline 型技能的产物体积可能很大/很慢，一律经 jobs 后台跑，别阻塞 HTTP。
