"""模型消息组装：系统提示词放最前，后面按顺序加入当前历史。"""
from copy import deepcopy


class MessageBuilder:
    """每步调用模型前，把通用历史展开成模型消息；不改变真实会话历史。"""
    def __init__(self, system=""):
        self.system = system

    def build(self, history):
        # 每步都从最新历史生成副本，保证上一步工具结果进入下一步模型输入。
        result = [{"role": "system", "content": self.system}] if self.system else []
        for item in history:
            result.append(deepcopy(item.message))
        return result
