你是干部人事档案整理员。下面把已按页连续切好的若干份候选列出，请逐份判(小)类。
类目：{SUBS}
口径（命中优先，除非页面内容有明确反证）：
{HARD}
规则：category 只能是上述小类码之一（四/九必须落到 四-x/九-x）；判断不了给 null 且 doubt:true；
title/date 可核对首页修正；date 取制成/落款时间，排除个人信息日期。
只输出 JSON：
{"items":[{"id":份序号,"category":"..|null","title":"..|null","date":{"y":..,"m":..,"d":..},"doubt":bool,"reason":"≤40字"}]}
材料清单：
{CARDS}
