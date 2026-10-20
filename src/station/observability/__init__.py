"""对话执行追踪：记录可观察步骤，不记录密钥、附件正文或模型思考过程。

新手视角：trace 是一条消息的执行档案，span 是档案里的一个步骤。
模块只保存时间、状态和有界元数据；业务函数仍沿原来的调用链执行。
"""
from .recorder import (activate, begin, bind, current, fail, fingerprint, finish,
                       first_text, input_info, isolated_stream, model_info, span)
