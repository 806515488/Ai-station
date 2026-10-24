"""会话内存历史与卡片位置适配；持久化由会话用例负责。"""
from station.core.contracts import HistoryItem, identity
from copy import deepcopy


class HistoryAdapter:
    """Thread 的内存视图；消息修改在此完成，数据库保存由会话用例负责。"""
    def __init__(self, thread):
        self.thread = thread
        self.revision = 0  # 本次适配器内的修改版本，用于检查点校验，不是数据库版本。
        self._raw = {}  # item_id → 原始消息，保留 trace_id 等宿主字段。
        self._items = []  # 给 Loop 的干净消息视图，只含模型协议字段。
        for message in thread.msgs:
            item_id = identity()
            self._raw[item_id] = message
            # trace_id 等展示/追踪元数据保留在 Thread，不作为模型输入。
            clean = {k: deepcopy(v) for k, v in message.items()
                     if k in {"role", "content", "tool_calls", "tool_call_id", "name"}}
            self._items.append(HistoryItem(item_id, clean))

    def snapshot(self):
        """给调用方一份副本；改这份快照不会悄悄改掉会话或绕过 revision。"""
        return deepcopy(self._items)

    def append(self, message, *, item_id=""):
        # 同时更新原始 Thread 和通用视图，但不保存；Thread.add 会自动补当前 trace_id。
        item = HistoryItem(item_id or identity(), deepcopy(message))
        self.thread.add(deepcopy(message))
        self._raw[item.item_id] = self.thread.msgs[-1]
        self._items.append(item)
        self.revision += 1
        return item

    def replace(self, items, report):
        # 先完整计算替换结果；出错时原 Thread 和历史视图均不变化。
        if len({i.item_id for i in items}) != len(items):
            raise ValueError("历史项身份重复")
        raw = {}
        for item in items:
            original = self._raw.get(item.item_id)
            # 保留项沿用宿主元数据；新摘要没有旧消息，就由当前追踪补来源信息。
            raw[item.item_id] = {**(original or {}), **deepcopy(item.message)}
            if original is None:
                from station import observability as obs
                trace = obs.current()
                if trace and trace.record['user_id'] == self.thread.user_id and trace.record['thread_id'] == self.thread.id:
                    raw[item.item_id]['trace_id'] = trace.record['id']
        # 压缩会改变消息下标，历史卡片必须按报告重定位，否则刷新后会挂错位置。
        cards = deepcopy(self.thread.meta.get("cards", []))
        for card in cards:
            position = int(card.get("idx") or 0)
            card["idx"] = report.positions.get(position, max(0, len(items) - 1))
        # 只留有限条压缩审计，记录哪些消息被摘要替换。
        reports = [*self.thread.meta.get("compactions", []), {
            "removed_item_ids": list(report.removed_item_ids[:128]),
            "kept_item_ids": list(report.kept_item_ids[:128]), "removed_item_count": len(report.removed_item_ids),
            "reason": report.reason}][-20:]
        self.thread.msgs = [raw[item.item_id] for item in items]
        self.thread.meta.update(cards=cards, compactions=reports)
        self._items, self._raw = list(items), raw
        self.revision += 1

    def get_pending(self):
        """pending 保存待批准工具及原参数；返回副本，读取不改变原记录。"""
        return deepcopy(self.thread.pending)

    def set_pending(self, tool, arguments, preview=""):
        approval_id = self.thread.set_pending(tool, arguments, preview)
        self.revision += 1
        return approval_id

    def clear_pending(self):
        self.thread.clear_pending()
        self.revision += 1

    def remember(self, presentations):
        """检查所有卡片锚点后写入内存元数据；外层紧接着保存，再把卡片发给界面。"""
        positions = {item.item_id: i for i, item in enumerate(self._items)}
        additions = []
        for p in presentations:
            if p.anchor.item_id not in positions:
                raise ValueError("展示锚点不存在")
            if p.event.type == "render":
                additions.append({"idx": positions[p.anchor.item_id], "card": deepcopy(p.event.data["card"])})
        if additions:
            self.thread.meta['cards'] = [*self.thread.meta.get('cards', []), *additions][-30:]
