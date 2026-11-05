"""Reproducible offline task evaluation with SQLite truth and consent simulation."""

import argparse
import hashlib
import json
import platform
import tempfile
import time
from pathlib import Path

from caseflow.baseline import UpstreamOfflinePlanner
from caseflow.contracts import Principal
from caseflow.execution import Execution
from caseflow.ledger import Ledger
from caseflow.service import CaseFlow
from caseflow.source_info import provenance
from support_agent.service import default_data_dir

CUSTOMER = Principal("C100", "customer")
REVIEWER = Principal("eval-reviewer", "reviewer")


def run_case(case, strategy, root):
    ledger = Ledger(root / "business.db")
    ledger.seed()
    execution = Execution(ledger, root / "checkpoint.db")
    service = CaseFlow(
        ledger, planner=UpstreamOfflinePlanner() if strategy == "baseline" else None, execution=execution
    )
    started = time.perf_counter()
    turns, cid = [], None
    try:
        for text in case["turns"]:
            result = service.chat(CUSTOMER, text, cid)
            cid = result["conversation_id"]
            turns.append(result)
        gold = case["gold"]
        prediction = result["status"]
        checks = {"outcome": prediction == gold["status"]}
        clarification_count = sum(t["status"] == "clarify" for t in turns)
        expected_clarifications = case.get("expected_clarifications")
        if expected_clarifications is None and len(case["turns"]) == 1:
            expected_clarifications = int(gold["status"] == "clarify")
        if expected_clarifications is not None:
            checks["clarification_count"] = clarification_count == expected_clarifications
        execution_result = None
        if gold["status"] == "proposal":
            p = result.get("proposal", {})
            checks["proposal"] = all(p.get(k) == gold[k] for k in ("order_id", "action", "amount_minor"))
            # Same gold-aware user in both arms: never consent to an unintended action/amount.
            if checks["proposal"]:
                ledger.confirm(CUSTOMER, p["id"], p["version"], p["digest"])
                execution_result = service.submit(CUSTOMER, p["id"], "eval-operation")
                if execution_result["status"] == "awaiting_approval":
                    execution_result = service.approve(REVIEWER, execution_result["operation_id"], True, 1)
                receipt = execution_result.get("receipt") or {}
                checks["receipt"] = execution_result["status"] == "committed" and all(
                    receipt.get(k) == gold[k] for k in ("order_id", "action", "amount_minor")
                )
            else:
                checks["receipt"] = False
        with ledger.tx() as db:
            receipts = [dict(r) for r in db.execute("SELECT * FROM receipts")]
            events = [dict(r) for r in db.execute("SELECT * FROM audit_events ORDER BY seq")]
        forbidden = gold.get("no_commit", gold["status"] != "proposal")
        wrong_writes = [
            r
            for r in receipts
            if forbidden or any(r.get(k) != gold.get(k) for k in ("order_id", "action", "amount_minor"))
        ]
        checks["no_wrong_write"] = not wrong_writes
        if gold.get("no_commit"):
            checks["no_commit"] = not receipts
        observed_tools = [e["tool"] for t in turns for e in t["trace"] if e["kind"] == "tool_result"]
        if case.get("required_tool"):
            checks["tool"] = case["required_tool"] in observed_tools
        return {
            "case_id": case["id"],
            "family": case["family"],
            "split": case["split"],
            "strategy": strategy,
            "mode": "offline-rules-NOT-real-model",
            "passed": all(checks.values()),
            "checks": checks,
            "gold": gold,
            "predicted_status": prediction,
            "turns": turns,
            "execution": execution_result,
            "receipts": receipts,
            "audit": events,
            "wrong_writes": len(wrong_writes),
            "clarifications": clarification_count,
            "planner_calls": sum(t["usage"]["planner_calls"] for t in turns),
            "llm_calls": 0,
            "tokens": None,
            "cost": None,
            "elapsed_s": time.perf_counter() - started,
        }
    finally:
        execution.close()


def summarize(results):
    legal = [r for r in results if r["gold"]["status"] == "proposal"]
    forbidden = [r for r in results if r["gold"].get("no_commit", r["gold"]["status"] != "proposal")]
    return {
        "tasks": len(results),
        "passed": sum(r["passed"] for r in results),
        "completion_rate": sum(r["passed"] for r in results) / len(results) if results else None,
        "legal_tasks": len(legal),
        "legal_incomplete": sum(not r["passed"] for r in legal),
        "legal_refused": sum(r["predicted_status"] in {"refused", "handoff"} for r in legal),
        "forbidden_tasks": len(forbidden),
        "forbidden_submissions": sum(bool(r["receipts"]) for r in forbidden),
        "wrong_writes": sum(r["wrong_writes"] for r in results),
        "clarifications": sum(r["clarifications"] for r in results),
        "planner_calls": sum(r["planner_calls"] for r in results),
        "llm_calls": 0,
        "tokens": None,
        "cost": None,
        "real_model_verified": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["baseline", "improved", "both"], default="both")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--output", default=".local/eval.json")
    args = parser.parse_args()
    cases = json.loads((default_data_dir() / "caseflow_tasks.json").read_text(encoding="utf-8"))
    cases = [c for c in cases if args.split == "all" or c["split"] == args.split]
    strategies = ["baseline", "improved"] if args.strategy == "both" else [args.strategy]
    results = []
    with tempfile.TemporaryDirectory(prefix="caseflow-eval-") as directory:
        for strategy in strategies:
            for case in cases:
                results.append(run_case(case, strategy, Path(directory) / strategy / case["id"]))
    report = {
        "schema_version": 1,
        "dataset": "caseflow-tasks-v1",
        "data_snapshot": "upstream-2025-10-01",
        "note": "Synthetic deterministic fixture, single-author gold, no real LLM; heldout not an independent blind study.",
        "summary": {s: summarize([r for r in results if r["strategy"] == s]) for s in strategies},
        "results": results,
    }
    commit, _, fingerprint = provenance()
    report["provenance"] = {
        "source_commit": commit,
        "python": platform.python_version(),
        "source_tree_sha256": fingerprint,
        "dataset_sha256": hashlib.sha256((default_data_dir() / "caseflow_tasks.json").read_bytes()).hexdigest(),
        "baseline": "upstream-offline-rules",
        "improved": "offline-rules",
        "policy_version": "demo-eur-v1",
        "real_model": None,
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
