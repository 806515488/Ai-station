"""语音识别统一接口与协议分发。

技能业务只依赖这里返回的 Transcript 结构，不直接认识百炼、OpenAI 或别的厂商。
当前按模型名分发：
  * 含 ``filetrans`` 的 Qwen/Fun/Paraformer 模型 → DashScope 异步文件转写协议；
  * 其它协议暂未接入，会明确报错，不会假装已经转写。

这样以后接 OpenAI ``/audio/transcriptions`` 或其它厂商时，只需要再加一个协议模块，
不用改面试复盘工具和上层对话逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, TypedDict


_HERE = Path(__file__).resolve().parent
# registry 正常装载技能时会把这个目录放进 sys.path；这里再兜一次，
# 让测试或独立调用也能直接 import 同目录的协议模块。
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


class TranscriptSegment(TypedDict, total=False):
    start: float
    end: float
    speaker: str
    text: str
    confidence: float


class Transcript(TypedDict, total=False):
    text: str
    language: str
    provider: str
    model: str
    task_id: str
    segments: list[TranscriptSegment]


def _owner(ctx) -> str:
    """对话场景优先取 thread.user_id，后台场景回落 ctx.user_id。"""
    t = getattr(ctx, "thread", None)
    return ((getattr(t, "user_id", "") if t else "")
            or getattr(ctx, "user_id", "") or "")


def resolve_entry(ctx) -> dict:
    """取当前用户「语音识别模型」槽位的首条配置。"""
    from station import modelcfg
    entries = modelcfg.resolve(_owner(ctx), "speech")
    if not entries:
        raise RuntimeError(
            "还没配置「语音识别模型」。请在右上角 ⚙ 模型配置里添加一个 ASR 来源，"
            "填写接口地址、模型名和密钥，再把它选进「语音识别模型」这一行。")
    return entries[0]


def transcribe_entry(entry: dict, audio_url: str, *,
                     language_hints: list[str] | None = None,
                     diarization_enabled: bool = True,
                     channel_id: int = 0,
                     poll_interval: float = 5.0,
                     poll_deadline: float = 7200.0,
                     on_progress: Callable[[dict], None] | None = None) -> Transcript:
    """按 entry.model 分发给具体协议；当前支持 DashScope Filetrans 系列。"""
    for ev in iter_transcribe_entry(
            entry, audio_url,
            language_hints=language_hints,
            diarization_enabled=diarization_enabled,
            channel_id=channel_id,
            poll_interval=poll_interval,
            poll_deadline=poll_deadline):
        if ev.get("type") == "progress":
            if on_progress:
                try:
                    on_progress({
                        "percent": int(ev.get("percent") or 0),
                        "message": str(ev.get("message") or ""),
                        **({"task_id": ev["task_id"]} if ev.get("task_id") else {}),
                    })
                except Exception:          # noqa：显示层坏掉不影响转写
                    pass
        elif ev.get("type") == "result":
            return ev["result"]
    raise RuntimeError("语音识别没有返回结果")


def iter_transcribe_entry(entry: dict, audio_url: str, *,
                          language_hints: list[str] | None = None,
                          diarization_enabled: bool = True,
                          channel_id: int = 0,
                          poll_interval: float = 5.0,
                          poll_deadline: float = 7200.0,
                          existing_task_id: str = "", checkpoint=None):
    """生成器版：按协议产出 progress，最后产出 result。"""
    model = str(entry.get("model") or "").strip()
    lower = model.lower()
    if lower.startswith("qwen-audio-") and "filetrans" in lower:
        import dashscope_filetrans

        yield from dashscope_filetrans.iter_transcribe(
            entry.get("base_url") or "",
            entry.get("api_key") or "",
            model,
            audio_url,
            language_hints=language_hints,
            diarization_enabled=diarization_enabled,
            channel_id=channel_id,
            poll_interval=poll_interval,
            poll_deadline=poll_deadline,
            **({"existing_task_id":existing_task_id,"checkpoint":checkpoint} if existing_task_id or checkpoint else {}),
        )
        return

    raise RuntimeError(
        f"暂不支持语音识别模型 {model or '(未填写)'} 的调用协议。"
        "当前只接入了 Qwen-Audio-3.x Filetrans 异步协议。")


def transcribe(ctx, audio_url: str, *,
               language_hints: list[str] | None = None,
               diarization_enabled: bool = True,
               channel_id: int = 0,
               poll_interval: float = 5.0,
               poll_deadline: float = 7200.0,
               on_progress: Callable[[dict], None] | None = None) -> Transcript:
    """从当前用户配置解析语音模型，并把公网音频 URL 转成统一转写结果。"""
    entry = resolve_entry(ctx)
    return transcribe_entry(
        entry,
        audio_url,
        language_hints=language_hints,
        diarization_enabled=diarization_enabled,
        channel_id=channel_id,
        poll_interval=poll_interval,
        poll_deadline=poll_deadline,
        on_progress=on_progress,
    )


def iter_transcribe(ctx, audio_url: str, *,
                    language_hints: list[str] | None = None,
                    diarization_enabled: bool = True,
                    channel_id: int = 0,
                    poll_interval: float = 5.0,
                    poll_deadline: float = 7200.0):
    """从当前用户配置解析模型，并按协议产出可转发给 job 的事件。"""
    from station.tasks.runtime import entry_snapshot,resolve_remote
    recovery=getattr(ctx,"task_recovery",{}) or {}
    entry=resolve_remote(_owner(ctx),recovery,"speech") if recovery.get("remote_id") else resolve_entry(ctx)
    callback=getattr(ctx,"task_checkpoint",None)
    if callback:callback(**entry_snapshot(entry))
    yield from iter_transcribe_entry(
        entry, audio_url,
        language_hints=language_hints,
        diarization_enabled=diarization_enabled,
        channel_id=channel_id,
        poll_interval=poll_interval,
        poll_deadline=poll_deadline,
        **({"existing_task_id":recovery.get("remote_id", ""),"checkpoint":callback} if callback or recovery.get("remote_id") else {}),
    )
