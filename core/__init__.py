"""排产助手核心模块。

模块划分（N4 落地）：
- reader   读三个 sheet
- cleaner  洗脏数据 + 体检记录
- instance 订单 → 工序任务
- solver   规则 / CP-SAT 排程
- checker  四条硬约束校验（不重不漏 / 机器对 / 不倒序 / 不重叠）
- report   排单表 / 甘特 / 风险 / 体检
"""
