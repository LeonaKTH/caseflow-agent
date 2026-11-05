"""Conservative proposal checks, independent of planner and evaluation gold.

This is a veto on recognizable conflicts, not a general natural-language parser.
Unknown refund amounts require an explicit amount/full instruction. Human consent
still binds the final order/action/amount; passing this guard proves no NLU claim.
"""

import json
import re
import unicodedata

from caseflow.contracts import Conflict, minor_units

VERSION = "proposal-semantics-v1"
IDS = re.compile(r"(?<!\d)\d{4}(?!\d)")
ACTION = re.compile(r"cancel|取消|refund|return|退款|退掉|退货|改退|退(?=\d)", re.I)
FULL = re.compile(r"全额|\bfull\b", re.I)
NEGATIVE = re.compile(
    r"不需要|不必|不用|无需|无须|不要|不想|不再|别|不是|并非|不能|不[要退取消全]|\bnot\b|\bnever\b|\bno\b|don't|do not",
    re.I,
)
VAGUE = re.compile(
    r"大概|大约|左右|随便|看着办|尽量|最多|最大|至少|以内|不超过|不低于|\babout\b|\baround\b|\bbetween\b|as much|up to|at least|max(?:imum)?",
    re.I,
)
REFERENCE = re.compile(r"那个|这单|另一|前一|后一|第[一二三]|\bthat\b|\bother\b|\bfirst\b|\bsecond\b", re.I)
SELECTOR = re.compile(r"较大|largest|bigger|昨天|yesterday", re.I)
MONEY = re.compile(
    r"(?:EUR|€)\s*(?P<pre>\d+(?:\.\d+)?)|(?P<post>\d+(?:\.\d+)?)\s*(?P<unit>EUR|euros?|€|欧元|欧|CNY|人民币|元|USD|美元)",
    re.I,
)


def clauses(text):
    return re.split(r"[，,；;。]|\bbut\b|\binstead\b|改成|改为", text)


def negated(clause, position):
    # A negative in a previous clause cannot negate a later replacement action.
    prefix = clauses(clause[:position])[-1]
    return bool(NEGATIVE.search(prefix[-24:]))


def amounts(text):
    found = []
    for clause in clauses(text):
        for m in MONEY.finditer(clause):
            unit = (m.group("unit") or "EUR").lower()
            currency = "CNY" if unit in {"cny", "人民币", "元"} else "USD" if unit in {"usd", "美元"} else "EUR"
            found.append((minor_units(m.group("pre") or m.group("post")), currency, negated(clause, m.start())))
    return found


def explicit_full(text):
    flags = [not negated(c, m.start()) for c in clauses(text) for m in FULL.finditer(c)]
    return bool(flags) and all(flags) and not NEGATIVE.search(text)


def prior_candidates(history):
    for message in reversed(history):
        if message["role"] == "assistant":
            try:
                result = json.loads(message["content"])
                ps = result.get("proposals", []) or ([result["proposal"]] if result.get("proposal") else [])
                return {p["order_id"] for p in ps}
            except (ValueError, TypeError, KeyError, AttributeError):
                return set()
    return set()


def proposal_issue(decision, text, context, history, order=None):
    """Return a stable clarification code, or None. Never changes model arguments."""
    text = unicodedata.normalize("NFKC", text).lower()
    ids = set(IDS.findall(text))
    if not ids and REFERENCE.search(text) and not SELECTOR.search(text):
        if (
            len(prior_candidates(history)) > 1
            or not context.get("order_id")
            or re.search(r"两个|两件|两单|一单|一件|\btwo\b|\bboth\b", text)
        ):
            return "AMBIGUOUS_ORDER_REFERENCE"
        if decision.order_id != context["order_id"]:
            return "ORDER_INTENT_MISMATCH"
    if ids and decision.order_id not in ids:
        return "ORDER_INTENT_MISMATCH"
    relevant = [c for c in clauses(text) if decision.order_id in IDS.findall(c)]
    if len(ids) <= 1:
        scoped = "，".join(c for c in clauses(text) if not IDS.findall(c) or decision.order_id in IDS.findall(c))
    else:
        scoped = "，".join(relevant) if relevant else text
    if len(ids) > 1 and any(len(set(IDS.findall(c))) > 1 for c in relevant) and not SELECTOR.search(text):
        return "AMBIGUOUS_ORDER_MAPPING"
    matches = list(ACTION.finditer(scoped))
    affirmative = {
        "cancel" if re.search(r"cancel|取消", m.group(), re.I) else "refund"
        for m in matches
        if not negated(scoped, m.start())
    }
    if matches and (decision.action not in affirmative):
        return "ACTION_INTENT_CONFLICT"
    if decision.action == "cancel":
        return None
    if decision.action != "refund":
        return None  # Repository rejects unknown actions.
    if (
        not matches
        and context.get("action") != "refund"
        and not any(m["role"] == "user" and ACTION.search(m["content"]) for m in history[-2:])
    ):
        return "ACTION_INTENT_CONFLICT"
    if decision.full_refund and decision.amount is not None:
        return "CONFLICTING_REFUND_FIELDS"
    if VAGUE.search(scoped):
        return "AMBIGUOUS_REFUND_AMOUNT"
    source = scoped
    # A slot-only reply may complete the last explicit request. Never carry an
    # amount to a newly selected order or across a fresh operation instruction.
    if not MONEY.search(source) and not FULL.search(source) and not matches:
        can_inherit = (not ids or context.get("order_id") is None) and not (
            ids and context.get("order_id") and decision.order_id != context["order_id"]
        )
        if can_inherit:
            source = next((m["content"] for m in reversed(history) if m["role"] == "user"), source)
    full = list(FULL.finditer(source))
    affirmative_full = explicit_full(source)
    negative_full = any(negated(source, m.start()) for m in full)
    try:
        requested = amounts(source)
    except Conflict:
        return "EXPLICIT_REFUND_AMOUNT_REQUIRED"
    if decision.full_refund:
        if not affirmative_full or negative_full:
            return "EXPLICIT_FULL_REFUND_REQUIRED"
        values = {(v, c) for v, c, negative in requested if not negative}
        if values and (not order or values != {(order["total_minor"], order["currency"])}):
            return "CONFLICTING_USER_AMOUNT"
        return None
    try:
        value = minor_units(decision.amount)
    except Conflict:
        return "EXPLICIT_REFUND_AMOUNT_REQUIRED"
    positive = {(v, c) for v, c, negative in requested if not negative}
    if (
        affirmative_full
        and not negative_full
        and positive
        and order
        and positive != {(order["total_minor"], order["currency"])}
    ):
        return "CONFLICTING_USER_AMOUNT"
    if not positive and affirmative_full and not negative_full and order and value == order["total_minor"]:
        return None
    if len(positive) != 1 or (len(requested) > 1 and re.search(r"\bor\b|或者|或|到|至", source)):
        return "AMBIGUOUS_REFUND_AMOUNT"
    if (value, decision.currency) not in positive:
        return "AMOUNT_INTENT_MISMATCH"
    return None
