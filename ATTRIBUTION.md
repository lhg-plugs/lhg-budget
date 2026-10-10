# 灵感来源与致谢（Attribution）

本插件的**方法论灵感**来自以下项目，**所有实现代码、检测规则与文案均为原创**，未复制任何第三方的原文案或代码。

| 项目 | 作者 / 组织 | 许可（2026-10-11 核实） | 链接 | 借鉴了什么思路 |
|---|---|---|---|---|
| finance-assistant-skill | felciano | 自定义许可（非宽松许可） | https://github.com/felciano/finance-assistant-skill | 仅借鉴"账单导入五步"高层流程思路（格式探测→解析→预览→确认→去重）；**该项目的文案、提示词、代码一律未复制**，与其无任何派生关系 |
| expense-tracker-skill | Ahtisham Dilawar | MIT | https://github.com/ahtishamdilawar/expense-tracker-skill | 仅借鉴"确定性 CLI + 待确认队列"与"非投资建议"声明的高层思路；实现与文案均为原创 |
| budgetai | krusol21 | 未声明许可 | https://github.com/krusol21/budgetai | 仅借鉴"CSV 自动识别 + 重复导入检测 + 按日均推算预测"的高层思路；代码与文案一律未复制 |
| personal-finance-skill | kakalition | MIT | https://github.com/kakalition/personal-finance-skill | 仅借鉴"本地优先、Python 标准库零依赖"的高层思路；实现为原创 |
| actual | actualbudget | MIT | https://github.com/actualbudget/actual | 仅借鉴"信封预算法"这一通用理财常识框架；本插件的方法论表述为原创 |
| lhg-money-plan | lhg-plugs（本组织） | MIT | https://github.com/lhg-plugs/lhg-money-plan | 差异化定位参考：money-plan 做"理财规划"（分配/目标），本插件做"账单导入执行"（导入→分类→报表→预警），两者互补不重复；合规三层防护设计对齐组织惯例，实现各自原创 |

## 声明

1. **借鉴思路，实现与文案均为原创**：以上项目的具体文案、提示词原文、代码实现均未被复制。本插件的 skill 方法论文案、审计规则、命令流程、hook 脚本均为独立撰写。
2. **许可合规**：本插件审计脚本与 hook 仅使用 Python 标准库，未捆绑任何第三方包；未复制任何非宽松许可项目的代码或文案。
3. **商标与归属**：各项目名称与作者署名归其各自所有者所有。
4. **合规定位**：本插件仅为记账与预算分析助手，不构成任何投资建议；涉及投资决策请咨询持牌专业人士。

## 本插件自身的许可

本插件原创部分采用 MIT 许可（见 `LICENSE` 文件），版权所有：刘洪光（https://github.com/lhg-skills）。
