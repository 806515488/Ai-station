"""导出层：4 件套（xlsx/原件PDF/待核对清单/自动判定报告）。已含 excel/original_pdf/reports。

新手视角（Java 朋友版）：export = archive 的“报表导出器”目录。三个子模块各负责一种
产物，入口函数都形如 export_xxx(数据, person, 输出目录) → 返回生成的文件路径。
"""
