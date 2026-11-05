"""Shared process-local admission budget; never refunds uncertain/failed requests."""

import json
import os
import threading
from decimal import Decimal
from urllib.parse import urlsplit


class Quota:
    def __init__(
        self, max_calls, budget=None, input_price=None, output_price=None, *, max_input_bytes=None, input_overhead=0
    ):
        self.limit, self.calls = max_calls, 0
        self.budget = Decimal(str(budget)) if budget is not None else None
        self.input_price = Decimal(str(input_price or 0))
        self.output_price = Decimal(str(output_price or 0))
        self.reserved = Decimal(0)
        self.max_input_bytes, self.input_overhead = max_input_bytes, input_overhead
        self.last_error = None
        self._lock = threading.Lock()
        if (
            self.limit < 1
            or not all(p.is_finite() and p >= 0 for p in (self.input_price, self.output_price))
            or (self.budget is not None and (not self.budget.is_finite() or self.budget <= 0))
        ):
            raise ValueError("Invalid evaluation quota/pricing")

    def reserve(self, messages, max_output):
        size = len(json.dumps(messages, ensure_ascii=False).encode())
        reserve = ((size + self.input_overhead) * self.input_price + max_output * self.output_price) / 1_000_000
        with self._lock:
            if self.max_input_bytes is not None and size > self.max_input_bytes:
                self.last_error = "INPUT_BUDGET"
                return False
            if self.calls >= self.limit or (self.budget is not None and self.reserved + reserve > self.budget):
                self.last_error = "SESSION_BUDGET"
                return False
            self.calls += 1
            self.reserved += reserve
            return True

    @classmethod
    def from_env(cls, settings):
        official = urlsplit(settings.base_url).hostname == "api.deepseek.com" and settings.model == "deepseek-flash"
        ip = os.getenv("CASEFLOW_INPUT_PRICE_PER_M", "2" if official else "")
        op = os.getenv("CASEFLOW_OUTPUT_PRICE_PER_M", "8" if official else "")
        currency = os.getenv("CASEFLOW_PRICE_CURRENCY", "CNY" if official else "")
        calls = int(os.getenv("CASEFLOW_MAX_REQUESTS", "48"))
        budget = Decimal(os.getenv("CASEFLOW_BUDGET_CNY", "5"))
        if (
            not ip
            or not op
            or currency != "CNY"
            or not 1 <= calls <= 48
            or not budget.is_finite()
            or not 0 < budget <= 5
        ):
            raise ValueError("Live runtime requires reviewed CNY prices, <=48 requests and <=5 CNY budget")
        if not all(p.is_finite() and p >= 0 for p in (Decimal(ip), Decimal(op))):
            raise ValueError("Invalid live prices")
        if official and (Decimal(ip) < 2 or Decimal(op) < 8):
            raise ValueError("DeepSeek smoke reserve must use audited peak cache-miss prices (2/8 CNY per million)")
        return cls(calls, budget, ip, op, max_input_bytes=24_000, input_overhead=512)
