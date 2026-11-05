"""Runs the conversation scenarios in data/scenarios.json and checks tools, outcome and refunds.

python -m support_agent.evaluate [--provider openai]

Each scenario runs in a fresh service, so one scenario's refund can't affect another.
"""

from __future__ import annotations

import argparse
import json

from support_agent.models import get_model
from support_agent.service import SupportService, default_data_dir


def run_scenarios(provider: str | None = None) -> list[dict]:
    scenarios = json.loads((default_data_dir() / "scenarios.json").read_text(encoding="utf-8"))
    results = []
    for scenario in scenarios:
        service = SupportService(model=get_model(provider))
        result = service.chat(scenario["customer"], scenario["message"])
        checks = {
            "tools": result.tools_used == scenario["expect_tools"],
            "status": result.status == scenario["expect_status"],
            "reply": scenario.get("expect_reply", "").lower() in result.reply.lower(),
            "no_refund": not (scenario.get("expect_no_refund") and service.store.refunds),
        }
        results.append(
            {
                "name": scenario["name"],
                "passed": all(checks.values()),
                "failed_checks": [k for k, ok in checks.items() if not ok],
                "tools": result.tools_used,
                "status": result.status,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the support agent on conversation scenarios.")
    parser.add_argument("--provider", help="offline | openai | azure")
    results = run_scenarios(parser.parse_args().provider)
    for r in results:
        mark = "PASS" if r["passed"] else f"FAIL ({', '.join(r['failed_checks'])})"
        print(f"{mark:<22} {r['name']:<36} {r['status']:<18} {', '.join(r['tools'])}")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} scenarios passed")


if __name__ == "__main__":
    main()
