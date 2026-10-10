#!/usr/bin/env python3
"""lhg-budget 硬门：投资建议关键词拦截。

触发：PreToolUse / Write|Edit。扫描本次写入/编辑的文本内容，
若命中投资建议关键词（买入/加仓/必涨/抄底/重仓/推荐买等），
以 exit code 2 + JSON deny 拦截该操作，并给出改写指引。

逃生方式（按推荐度排序）：
  1. LHG_BUDGET_HOOKS_OFF=1（环境变量，当前会话有效；关闭后本拦截
     不再触发，但 budget_audit.py 的 B-INV-001 仍会在 audit 时报 blocker）。
  2. /plugin disable lhg-budget（整个插件停用，hooks 不再触发）。

设计原则：只有"命中关键词"才拦截；内部出错一律 fail-open
（直接放行，不报错、不骚扰）；零第三方依赖。

注意：本 hook 只做关键词层面的硬拦截，真正的合规判断
（上下文、语气）仍由 agent 按 skill 规范负责。
"""

import json
import os
import sys

# 投资建议关键词表（原创整理：常见荐股/荐基/荐币话术）
ADVICE_KEYWORDS = [
    "买入", "加仓", "必涨", "抄底", "重仓", "满仓",
    "推荐买", "强烈看好", "肯定涨", "稳赚", "保本",
    "闭眼买", "翻倍", "十倍股", "梭哈", "清仓买入",
    "赶紧买", "错过后悔", "财富密码",
]

REWRITE_GUIDE = (
    "改写指引：删除具体品种推荐与涨跌断言；只保留记账/预算/分类/报表内容；"
    "如需提示风险，用标准话术替代："
    "“本工具仅为记账与预算分析助手，不构成任何投资建议；"
    "涉及投资决策请咨询持牌专业人士。”"
)


def extract_text(tool_input):
    """从 Write/Edit 的输入中抽取待写入文本。"""
    parts = []
    for key in ("content", "new_string", "old_string", "file_text"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    return "\n".join(parts)


def find_hits(text):
    return [kw for kw in ADVICE_KEYWORDS if kw in text]


def main() -> int:
    # 逃生开关
    if os.environ.get("LHG_BUDGET_HOOKS_OFF") == "1":
        return 0

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # 输入解析失败：fail-open

    tool_input = payload.get("tool_input", {}) or {}
    hits = find_hits(extract_text(tool_input))
    if not hits:
        return 0  # 未命中：放行

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "lhg-budget 合规硬门：检测到投资建议关键词 %s。%s"
                % ("、".join(hits), REWRITE_GUIDE)
            ),
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
