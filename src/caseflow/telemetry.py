"""Only explicit provider billing fields count as actual cost, never estimates."""

from decimal import Decimal, InvalidOperation


def reported_cost(usage):
    if not isinstance(usage, dict):
        return None
    amount = usage.get("cost")
    currency = usage.get("cost_currency", usage.get("currency"))
    if isinstance(amount, bool) or amount is None or not isinstance(currency, str) or not currency.strip():
        return None
    try:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0:
            return None
    except InvalidOperation:
        return None
    return {"amount": str(amount), "currency": currency.upper()}


def sum_costs(costs):
    if not costs or any(c is None for c in costs) or len({c["currency"] for c in costs}) != 1:
        return None
    return {"amount": str(sum((Decimal(c["amount"]) for c in costs), Decimal(0))), "currency": costs[0]["currency"]}
