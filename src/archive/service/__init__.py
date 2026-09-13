"""用例层：任务运行/审核状态机/确认生成（领域服务层）。

新手视角（Java 朋友版）：service ≈ Spring 的 Service 层——放“业务用例/事务编排”
的地方。当前识别主链路已由 station jobs + archive.station_adapter 承载；
后续“审核状态机 / 确认后导出”等用例再放这里（现为占位空壳）。
"""
