"""Small bilingual lexical index; no embedding or model download."""

import re

from support_agent.policies_kb import PolicyBase
from support_agent.service import default_data_dir


class PolicySearch:
    def __init__(self):
        self.english = PolicyBase(default_data_dir() / "policies.md")
        self.chinese = PolicyBase(default_data_dir() / "policies_zh.md")

    @staticmethod
    def terms(text):
        text = text.lower().replace("撤单", "取消").replace("退钱", "退款").replace("快递", "物流")
        terms = set(re.findall(r"[a-z0-9]+", text))
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            terms.update(run[i : i + 2] for i in range(len(run) - 1))
        return terms

    def search(self, query):
        wanted = self.terms(query)
        scored = [(len(wanted & self.terms(h + " " + b)), h, b) for h, b in self.chinese.sections]
        hits = [(h, b) for score, h, b in sorted(scored, reverse=True) if score > 0][:2]
        return hits or self.english.search(query)
