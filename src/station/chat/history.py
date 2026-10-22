"""保存对话展示事件：卡片锚定消息位置，正文流不逐字写数据库。"""
from station.core.events import Event


def remember_card(thread, card: dict) -> None:
    """卡片不进入模型正文；idx 供历史回放恢复它原来的展示位置。"""
    if not isinstance(card, dict):
        return
    cards = thread.meta.setdefault("cards", [])
    cards.append({"idx": len(thread.msgs), "card": card})
    if len(cards) > 30:
        del cards[:-30]


def save_event(thread, event: Event) -> None:
    """卡片立即落盘；delta 尚未改变历史，无需反复全量保存。"""
    if event.type == "render":
        remember_card(thread, event.data.get("card"))
    if event.type != "delta":
        thread.save()
