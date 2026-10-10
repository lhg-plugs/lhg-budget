#!/usr/bin/env python3
"""lhg-budget 确定性审计：账单 CSV 体检。

检查 8 大类 35 条规则：编码 / CSV 结构 / 金额 / 日期 / 分类 /
交易异常 / 预算 / 合规。零第三方依赖。

用法：
  python3 budget_audit.py --target <目录> [--format json|text]

扫描 <目录> 下的 *.csv（账单）、budget.json（预算，可选）、
report*.md（月度报表，可选）。

退出码：2=有 blocker，1=有 warning，0=干净（nit 不影响）。
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime

SEV_ORDER = {"blocker": 0, "warning": 1, "nit": 2}

# 列名映射关键词：包含即命中
DATE_KEYS = ["交易时间", "创建时间", "交易日期", "记账日期", "日期", "时间"]
AMT_KEYS = ["金额"]
DIR_KEYS = ["收/支", "收支", "借贷", "方向"]
PAYEE_KEYS = ["交易对方", "对方户名", "对方", "商户", "商家", "摘要", "商品"]
CAT_KEYS = ["交易分类", "分类", "类别"]
NOTE_KEYS = ["备注", "商品名称", "商品", "用途"]

DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y%m%d", "%d/%m/%Y"]
COMPLIANCE_PHRASES = ["不构成投资建议", "仅供记账参考", "仅为记账与预算"]
ADVICE_KEYWORDS = [
    "买入", "加仓", "必涨", "抄底", "重仓", "满仓",
    "推荐买", "强烈看好", "肯定涨", "稳赚", "保本",
    "闭眼买", "翻倍", "十倍股", "梭哈", "清仓买入",
    "赶紧买", "错过后悔", "财富密码",
]

FULLWIDTH = str.maketrans("０１２３４５６７８９．，", "0123456789.,")


# ---------------------------------------------------------------- 数据结构

class Txn:
    __slots__ = ("date", "payee", "category", "direction",
                 "amount", "note", "src", "lineno")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class BillFile:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.encoding = None
        self.delimiter = ","
        self.header = []
        self.rows = []          # list[list[str]]
        self.colmap = {}        # 语义 -> 列索引
        self.error = None       # 解码失败等致命错误
        self.bad_rows = 0       # 列数不一致的行数
        self.txns = []


# ---------------------------------------------------------------- 解析

def detect_encoding(raw):
    try:
        raw[:65536].decode("utf-8")
        # 全量严格校验（BOM 视为 utf-8-sig 兼容）
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("gb18030")
        return "gb18030"
    except UnicodeDecodeError:
        return None


def detect_delimiter(header_line):
    counts = {d: header_line.count(d) for d in [",", ";", "\t", "|"]}
    return max(counts, key=counts.get)


def map_columns(header):
    colmap = {}
    for i, h in enumerate(header):
        h = h.strip()
        for sem, keys in (("date", DATE_KEYS), ("amount", AMT_KEYS),
                          ("direction", DIR_KEYS), ("payee", PAYEE_KEYS),
                          ("category", CAT_KEYS), ("note", NOTE_KEYS)):
            if sem not in colmap and any(k in h for k in keys):
                colmap[sem] = i
    return colmap


def clean_amount(raw):
    """返回 (value, needed_cleaning, ok)。"""
    s = raw.strip().translate(FULLWIDTH)
    needed = bool(re.search(r"[¥￥,'\"\s]", s)) or "(" in s or ")" in s
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = re.sub(r"[¥￥,'\"\s]", "", s)
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None, needed, False
    try:
        v = float(s)
    except ValueError:
        return None, needed, False
    return (-v if neg else v), needed, True


def parse_date(raw):
    fmts_used = []
    s = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date(), fmt
        except ValueError:
            continue
    return None, None


def load_bill(path):
    bf = BillFile(path)
    with open(path, "rb") as f:
        raw = f.read()
    if not raw.strip():
        bf.error = "empty"
        return bf
    enc = detect_encoding(raw)
    if enc is None:
        bf.error = "undecodable"
        return bf
    bf.encoding = enc
    text = raw.decode("utf-8-sig" if enc == "utf-8" else "gb18030")
    lines = text.splitlines()
    bf.delimiter = detect_delimiter(lines[0])
    try:
        reader = csv.reader(lines, delimiter=bf.delimiter)
        rows = [r for r in reader if any(c.strip() for c in r)]
    except Exception:
        bf.error = "parse_error"
        return bf
    if not rows:
        bf.error = "empty"
        return bf
    bf.header = [c.strip() for c in rows[0]]
    data = rows[1:]
    bf.bad_rows = sum(1 for r in data if len(r) != len(bf.header))
    bf.rows = [r for r in data if len(r) == len(bf.header)]
    bf.colmap = map_columns(bf.header)
    return bf


def build_txns(bf):
    """从 BillFile 抽取交易；返回 (txns, stats)。"""
    stats = {"amt_fail": 0, "amt_cleaned": 0, "date_fmts": set(),
             "date_fail": 0, "dir_inferred": 0, "dir_inverted": 0,
             "decimals_bad": 0}
    if bf.error or "amount" not in bf.colmap or "date" not in bf.colmap:
        return [], stats
    ai, di = bf.colmap["amount"], bf.colmap["date"]
    diri = bf.colmap.get("direction")
    payi = bf.colmap.get("payee")
    cati = bf.colmap.get("category")
    notei = bf.colmap.get("note")
    for ln, r in enumerate(bf.rows, start=2):
        val, cleaned, ok = clean_amount(r[ai])
        if not ok:
            stats["amt_fail"] += 1
            continue
        if cleaned:
            stats["amt_cleaned"] += 1
        if abs(val) != round(abs(val), 2):
            stats["decimals_bad"] += 1
        d, fmt = parse_date(r[di])
        if d is None:
            stats["date_fail"] += 1
            continue
        stats["date_fmts"].add(fmt)
        direction = None
        if diri is not None:
            dv = r[diri].strip()
            if dv in ("收入", "收", "贷", "入"):
                direction = "收入"
            elif dv in ("支出", "支", "借", "出"):
                direction = "支出"
            # 有明确方向列时，金额应为非负数；负号与方向列矛盾
            if val < 0:
                stats["dir_inverted"] += 1
        if direction is None:
            direction = "收入" if val >= 0 else "支出"
            stats["dir_inferred"] += 1
        bf.txns.append(Txn(
            date=d, payee=r[payi].strip() if payi is not None else "",
            category=r[cati].strip() if cati is not None else "",
            direction=direction, amount=abs(val),
            note=r[notei].strip() if notei is not None else "",
            src=bf.name, lineno=ln))
    return bf.txns, stats


# ---------------------------------------------------------------- 规则引擎

class Ctx:
    def __init__(self, target):
        self.target = target
        self.bills = []
        self.txns = []
        self.file_stats = {}   # path -> stats
        self.budget = None
        self.budget_path = None
        self.reports = []      # [(path, text)]
        self.findings = []


def F(rule_id, severity, group, msg, where="", line=0):
    return {"rule_id": rule_id, "severity": severity, "group": group,
            "message": msg, "file": where, "line": line}


RULES = []


def rule(rid, sev, group):
    def deco(fn):
        RULES.append({"id": rid, "severity": sev, "group": group, "fn": fn})
        return fn
    return deco


# ---- 编码 ----
@rule("B-ENC-001", "blocker", "编码")
def enc_undecodable(ctx):
    out = []
    for bf in ctx.bills:
        if bf.error == "undecodable":
            out.append(F("B-ENC-001", "blocker", "编码",
                         "文件无法解码（UTF-8/GB18030 均失败），疑似乱码或二进制混入，请重新导出",
                         bf.name))
    return out


@rule("B-ENC-002", "nit", "编码")
def enc_non_utf8(ctx):
    return [F("B-ENC-002", "nit", "编码",
              "文件为 %s 编码，已自动兼容；建议转存为 UTF-8" % bf.encoding, bf.name)
            for bf in ctx.bills
            if bf.encoding and bf.encoding != "utf-8"]


# ---- CSV 结构 ----
@rule("B-CSV-001", "warning", "CSV结构")
def csv_col_mismatch(ctx):
    out = []
    for bf in ctx.bills:
        if bf.error or not bf.rows:
            continue
        ratio = bf.bad_rows / (bf.bad_rows + len(bf.rows))
        if ratio > 0.2:
            out.append(F("B-CSV-001", "blocker", "CSV结构",
                         "%.0f%% 数据行列数与表头不一致，文件结构损坏" % (ratio * 100), bf.name))
        elif bf.bad_rows:
            out.append(F("B-CSV-001", "warning", "CSV结构",
                         "%d 行列数与表头不一致（已跳过），请检查备注中的逗号" % bf.bad_rows, bf.name))
    return out


@rule("B-CSV-002", "blocker", "CSV结构")
def csv_empty(ctx):
    return [F("B-CSV-002", "blocker", "CSV结构", "空文件或无表头", bf.name)
            for bf in ctx.bills if bf.error in ("empty",)]


@rule("B-CSV-003", "warning", "CSV结构")
def csv_delimiter(ctx):
    names = {",": "逗号", ";": "分号", "\t": "制表符", "|": "竖线"}
    return [F("B-CSV-003", "warning", "CSV结构",
              "分隔符为%s而非逗号，已自动识别" % names[bf.delimiter], bf.name)
            for bf in ctx.bills
            if not bf.error and bf.delimiter != ","]


@rule("B-CSV-004", "nit", "CSV结构")
def csv_dup_header(ctx):
    out = []
    for bf in ctx.bills:
        if bf.error:
            continue
        seen, dups = set(), set()
        for h in bf.header:
            if h in seen:
                dups.add(h)
            seen.add(h)
        if dups:
            out.append(F("B-CSV-004", "nit", "CSV结构",
                         "表头重复列：%s（已取第一列）" % "、".join(sorted(dups)), bf.name))
    return out


# ---- 金额 ----
@rule("B-AMT-001", "blocker", "金额")
def amt_no_column(ctx):
    return [F("B-AMT-001", "blocker", "金额", "找不到可识别的金额列，无法继续", bf.name)
            for bf in ctx.bills
            if not bf.error and "amount" not in bf.colmap]


@rule("B-AMT-002", "warning", "金额")
def amt_no_direction(ctx):
    return [F("B-AMT-002", "warning", "金额",
              "无收支方向列，方向由金额正负推断，请人工核对", bf.name)
            for bf in ctx.bills
            if not bf.error and "amount" in bf.colmap and "direction" not in bf.colmap]


@rule("B-AMT-003", "warning", "金额")
def amt_cleaned(ctx):
    out = []
    for bf in ctx.bills:
        n = ctx.file_stats.get(bf.path, {}).get("amt_cleaned", 0)
        if n:
            out.append(F("B-AMT-003", "warning", "金额",
                         "%d 笔金额含货币符号/千分位/全角字符，已清洗；建议源头规范" % n, bf.name))
    return out


@rule("B-AMT-004", "warning", "金额")
def amt_fail(ctx):
    out = []
    for bf in ctx.bills:
        st = ctx.file_stats.get(bf.path, {})
        n = st.get("amt_fail", 0)
        total = len(bf.rows)
        if n and total and n / total > 0.1:
            out.append(F("B-AMT-004", "blocker", "金额",
                         "%d 笔金额解析失败（占比 %.0f%%），请检查源文件" % (n, n / total * 100), bf.name))
        elif n:
            out.append(F("B-AMT-004", "warning", "金额",
                         "%d 笔金额解析失败，已列入失败清单" % n, bf.name))
    return out


@rule("B-AMT-005", "warning", "金额")
def amt_inverted(ctx):
    out = []
    for bf in ctx.bills:
        n = ctx.file_stats.get(bf.path, {}).get("dir_inverted", 0)
        if n:
            out.append(F("B-AMT-005", "warning", "金额",
                         "%d 笔收支方向与金额符号矛盾（疑似方向颠倒）" % n, bf.name))
    return out


@rule("B-AMT-006", "warning", "金额")
def amt_decimals(ctx):
    out = []
    for bf in ctx.bills:
        n = ctx.file_stats.get(bf.path, {}).get("decimals_bad", 0)
        if n:
            out.append(F("B-AMT-006", "warning", "金额",
                         "%d 笔金额超过 2 位小数" % n, bf.name))
    return out


# ---- 日期 ----
@rule("B-DATE-001", "blocker", "日期")
def date_no_column(ctx):
    return [F("B-DATE-001", "blocker", "日期", "找不到可识别的日期列，无法继续", bf.name)
            for bf in ctx.bills
            if not bf.error and "date" not in bf.colmap]


@rule("B-DATE-002", "warning", "日期")
def date_mixed_format(ctx):
    out = []
    for bf in ctx.bills:
        fmts = ctx.file_stats.get(bf.path, {}).get("date_fmts", set())
        if len(fmts) > 1:
            out.append(F("B-DATE-002", "warning", "日期",
                         "同一文件混用 %d 种日期格式，已统一解析，请确认" % len(fmts), bf.name))
    return out


@rule("B-DATE-003", "warning", "日期")
def date_multi_month(ctx):
    months = {(t.date.year, t.date.month) for t in ctx.txns}
    if len(months) > 1:
        return [F("B-DATE-003", "warning", "日期",
                    "账单横跨 %d 个月份，月报请先按自然月拆分" % len(months))]
    return []


@rule("B-DATE-004", "nit", "日期")
def date_future(ctx):
    today = datetime.now().date()
    bad = [t for t in ctx.txns if t.date > today]
    if bad:
        return [F("B-DATE-004", "nit", "日期",
                    "%d 笔日期晚于今天，疑似录入错误" % len(bad), bad[0].src, bad[0].lineno)]
    return []


# ---- 分类 ----
def _expenses(ctx):
    return [t for t in ctx.txns if t.direction == "支出"]


@rule("B-CAT-001", "warning", "分类")
def cat_coverage(ctx):
    exp = _expenses(ctx)
    if not exp:
        return []
    unc = [t for t in exp if not t.category or t.category in ("其他", "未分类", "Unknown")]
    ratio = len(unc) / len(exp)
    if ratio > 0.6:
        return [F("B-CAT-001", "blocker", "分类",
                    "未分类率 %.0f%%（%d/%d 笔），请先补分类再出报表" % (ratio * 100, len(unc), len(exp)))]
    if ratio > 0.3:
        return [F("B-CAT-001", "warning", "分类",
                    "未分类率 %.0f%%（%d/%d 笔），报表“其他”行会很大" % (ratio * 100, len(unc), len(exp)))]
    return []


@rule("B-CAT-002", "warning", "分类")
def cat_no_column(ctx):
    return [F("B-CAT-002", "warning", "分类", "无分类列，全部交易需走自动分类", bf.name)
            for bf in ctx.bills
            if not bf.error and "amount" in bf.colmap and "category" not in bf.colmap]


@rule("B-CAT-003", "nit", "分类")
def cat_too_coarse(ctx):
    exp = _expenses(ctx)
    if not exp:
        return []
    total = sum(t.amount for t in exp)
    by_cat = {}
    for t in exp:
        by_cat[t.category or "未分类"] = by_cat.get(t.category or "未分类", 0) + t.amount
    top = max(by_cat.items(), key=lambda kv: kv[1])
    if total > 0 and top[1] / total > 0.7:
        return [F("B-CAT-003", "nit", "分类",
                    "分类“%s”占支出 %.0f%%，分类过粗，建议细化" % (top[0], top[1] / total * 100))]
    return []


@rule("B-CAT-004", "warning", "分类")
def cat_name_weird(ctx):
    bad = {t.category for t in ctx.txns
           if t.category and (len(t.category) > 8 or re.search(r"[^\w·、]", t.category))}
    if bad:
        return [F("B-CAT-004", "warning", "分类",
                    "分类名不规范（超长或含特殊符号）：%s" % "、".join(sorted(bad)[:5]))]
    return []


@rule("B-CAT-005", "nit", "分类")
def cat_other_ratio(ctx):
    exp = _expenses(ctx)
    if not exp:
        return []
    total = sum(t.amount for t in exp)
    other = sum(t.amount for t in exp if t.category == "其他")
    if total > 0 and other / total > 0.15:
        return [F("B-CAT-005", "nit", "分类",
                    "“其他”分类占支出 %.0f%%，规则库偏薄" % (other / total * 100))]
    return []


# ---- 交易异常 ----
@rule("B-TXN-001", "warning", "交易异常")
def txn_large(ctx):
    exp = [t.amount for t in _expenses(ctx) if t.amount > 0]
    if len(exp) < 5:
        return []
    med = statistics.median(exp)
    out = []
    for t in _expenses(ctx):
        if med > 0 and t.amount > med * 5:
            out.append(F("B-TXN-001", "warning", "交易异常",
                         "异常大额：%s ¥%.2f（中位数 ¥%.2f 的 %.1f 倍），请确认"
                         % (t.payee or t.note or "未知", t.amount, med, t.amount / med),
                         t.src, t.lineno))
    return out


@rule("B-TXN-002", "warning", "交易异常")
def txn_dup_in_file(ctx):
    out = []
    for bf in ctx.bills:
        seen = {}
        for t in bf.txns:
            key = (str(t.date), round(t.amount, 2), t.payee)
            seen.setdefault(key, []).append(t)
        for key, ts in seen.items():
            if len(ts) > 1:
                out.append(F("B-TXN-002", "warning", "交易异常",
                             "疑似重复导入：%s %s ¥%.2f 出现 %d 次"
                             % (key[0], key[2] or "未知", key[1], len(ts)),
                             bf.name, ts[1].lineno))
    return out


@rule("B-DUP-001", "warning", "交易异常")
def txn_dup_cross_file(ctx):
    seen = {}
    for t in ctx.txns:
        key = (str(t.date), round(t.amount, 2), t.payee)
        seen.setdefault(key, []).append(t)
    out = []
    for key, ts in seen.items():
        srcs = {t.src for t in ts}
        if len(srcs) > 1:
            out.append(F("B-DUP-001", "warning", "交易异常",
                         "跨文件重复：%s %s ¥%.2f 同时出现在 %s"
                         % (key[0], key[2] or "未知", key[1], "、".join(sorted(srcs)))))
    return out


@rule("B-TXN-003", "nit", "交易异常")
def txn_refund_pair(ctx):
    by_payee = {}
    for t in ctx.txns:
        by_payee.setdefault(t.payee, []).append(t)
    out = []
    for payee, ts in by_payee.items():
        if not payee:
            continue
        amts = {}
        for t in ts:
            amts.setdefault(round(t.amount, 2), []).append(t.direction)
        for amt, dirs in amts.items():
            if "收入" in dirs and "支出" in dirs:
                out.append(F("B-TXN-003", "nit", "交易异常",
                             "疑似退款/冲销对：%s 有 ±¥%.2f 两笔，请确认已对冲" % (payee, amt),
                             ts[0].src))
                break
    return out


@rule("B-TXN-004", "warning", "交易异常")
def txn_over_total_budget(ctx):
    if not ctx.budget or not ctx.budget.get("total"):
        return []
    total = ctx.budget["total"]
    return [F("B-TXN-004", "warning", "交易异常",
              "单笔支出 ¥%.2f 超过月度总预算 ¥%.2f" % (t.amount, total), t.src, t.lineno)
            for t in _expenses(ctx) if t.amount > total]


@rule("B-TXN-005", "nit", "交易异常")
def txn_zero(ctx):
    bad = [t for t in ctx.txns if t.amount == 0]
    if bad:
        return [F("B-TXN-005", "nit", "交易异常",
                    "%d 笔金额为 0，多为状态行/冲销残留" % len(bad), bad[0].src, bad[0].lineno)]
    return []


# ---- 预算 ----
def _budget_map(ctx):
    b = ctx.budget or {}
    return b.get("budgets") or b.get("categories") or {}


@rule("B-BUD-001", "blocker", "预算")
def bud_over_category(ctx):
    bm = _budget_map(ctx)
    if not bm:
        return []
    spend = {}
    for t in _expenses(ctx):
        cat = t.category.split("·")[0] if t.category else "未分类"
        spend[cat] = spend.get(cat, 0) + t.amount
    out = []
    for cat, lim in bm.items():
        if lim and spend.get(cat, 0) > lim:
            out.append(F("B-BUD-001", "blocker", "预算",
                         "分类超支：%s 实际 ¥%.2f / 预算 ¥%.2f（%.0f%%）"
                         % (cat, spend[cat], lim, spend[cat] / lim * 100)))
    return out


@rule("B-BUD-002", "blocker", "预算")
def bud_over_total(ctx):
    if not ctx.budget or not ctx.budget.get("total"):
        return []
    total_spend = sum(t.amount for t in _expenses(ctx))
    if total_spend > ctx.budget["total"]:
        return [F("B-BUD-002", "blocker", "预算",
                    "月度总超支：实际 ¥%.2f / 总预算 ¥%.2f"
                    % (total_spend, ctx.budget["total"]))]
    return []


@rule("B-BUD-003", "warning", "预算")
def bud_missing(ctx):
    if ctx.txns and not ctx.budget:
        return [F("B-BUD-003", "warning", "预算", "未找到 budget.json，无法做超支预警")]
    return []


@rule("B-BUD-004", "warning", "预算")
def bud_high_usage(ctx):
    bm = _budget_map(ctx)
    if not bm:
        return []
    spend = {}
    for t in _expenses(ctx):
        cat = t.category.split("·")[0] if t.category else "未分类"
        spend[cat] = spend.get(cat, 0) + t.amount
    out = []
    for cat, lim in bm.items():
        if lim and lim > 0 and spend.get(cat, 0) / lim >= 0.8 and spend.get(cat, 0) <= lim:
            out.append(F("B-BUD-004", "warning", "预算",
                         "预算使用过快：%s 已用 %.0f%%（¥%.2f/¥%.2f）"
                         % (cat, spend[cat] / lim * 100, spend[cat], lim)))
    return out


@rule("B-BUD-005", "nit", "预算")
def bud_zero(ctx):
    bm = _budget_map(ctx)
    bad = [c for c, lim in bm.items() if lim is not None and lim <= 0]
    if bad:
        return [F("B-BUD-005", "nit", "预算",
                    "以下分类预算为 0 或负数：%s" % "、".join(bad))]
    return []


@rule("B-BUD-006", "warning", "预算")
def bud_concentrated(ctx):
    bm = _budget_map(ctx)
    total = sum(v for v in bm.values() if v)
    if total <= 0:
        return []
    top = max(bm.items(), key=lambda kv: kv[1] or 0)
    if top[1] / total > 0.6:
        return [F("B-BUD-006", "warning", "预算",
                    "预算集中在单一分类：%s 占总预算 %.0f%%" % (top[0], top[1] / total * 100))]
    return []


# ---- 合规 ----
@rule("B-INV-001", "blocker", "合规")
def inv_advice(ctx):
    out = []
    for path, text in ctx.reports:
        hits = [kw for kw in ADVICE_KEYWORDS if kw in text]
        if hits:
            out.append(F("B-INV-001", "blocker", "合规",
                         "报表含投资建议关键词（%s），请改写为事实描述" % "、".join(hits[:5]),
                         os.path.basename(path)))
    return out


@rule("B-CMP-001", "warning", "合规")
def cmp_statement(ctx):
    out = []
    for path, text in ctx.reports:
        if not any(p in text for p in COMPLIANCE_PHRASES):
            out.append(F("B-CMP-001", "warning", "合规",
                         "报表缺少合规声明，请在文末加上“本报告仅为记账与预算分析，不构成任何投资建议”",
                         os.path.basename(path)))
    return out


# ---------------------------------------------------------------- 主流程

def scan(target):
    ctx = Ctx(target)
    for name in sorted(os.listdir(target)):
        p = os.path.join(target, name)
        if not os.path.isfile(p):
            continue
        low = name.lower()
        if low.endswith(".csv"):
            bf = load_bill(p)
            ctx.bills.append(bf)
            txns, stats = build_txns(bf)
            ctx.file_stats[p] = stats
            ctx.txns.extend(txns)
        elif low == "budget.json":
            try:
                with open(p, encoding="utf-8") as f:
                    ctx.budget = json.load(f)
                ctx.budget_path = p
            except Exception as e:
                ctx.findings.append(F("B-CSV-002", "blocker", "预算",
                                       "budget.json 解析失败：%s" % e, name))
        elif low.startswith("report") and low.endswith(".md"):
            try:
                with open(p, encoding="utf-8") as f:
                    ctx.reports.append((p, f.read()))
            except Exception:
                pass
    return ctx


def run_audit(target):
    ctx = scan(target)
    for r in RULES:
        try:
            ctx.findings.extend(r["fn"](ctx))
        except Exception as e:  # 单规则失败不影响整体：fail-open
            ctx.findings.append(F(r["id"], "nit", r["group"],
                                  "规则执行异常（已跳过）：%s" % e))
    ctx.findings.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["rule_id"]))
    return ctx


def summarize(ctx):
    s = {"rules_run": len(RULES), "blocker": 0, "warning": 0, "nit": 0,
         "files": len(ctx.bills), "transactions": len(ctx.txns)}
    for f in ctx.findings:
        s[f["severity"]] += 1
    return s


def main():
    ap = argparse.ArgumentParser(description="lhg-budget 账单审计")
    ap.add_argument("--target", required=True, help="待体检目录")
    ap.add_argument("--format", choices=["json", "text"], default="text")
    args = ap.parse_args()

    if not os.path.isdir(args.target):
        print("目标目录不存在：%s" % args.target, file=sys.stderr)
        return 2

    ctx = run_audit(args.target)
    summary = summarize(ctx)

    if args.format == "json":
        print(json.dumps({"summary": summary, "findings": ctx.findings},
                         ensure_ascii=False, indent=2))
    else:
        print("lhg-budget 审计报告：%s" % args.target)
        print("规则 %d 条 ｜ 文件 %d ｜ 交易 %d 笔"
              % (summary["rules_run"], summary["files"], summary["transactions"]))
        print("blocker %d ｜ warning %d ｜ nit %d"
              % (summary["blocker"], summary["warning"], summary["nit"]))
        for f in ctx.findings:
            loc = f["file"] + (":%d" % f["line"] if f["line"] else "")
            print("[%s] %s %s %s" % (f["severity"].upper(), f["rule_id"], loc, f["message"]))

    if summary["blocker"]:
        return 2
    if summary["warning"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
