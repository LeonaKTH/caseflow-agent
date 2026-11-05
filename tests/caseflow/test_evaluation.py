from caseflow.evaluate import run_case, summarize


def test_eval_checks_sqlite_receipt_and_labels_mock(tmp_path):
    case = {
        "id": "sample",
        "family": "explicit",
        "split": "dev",
        "turns": ["refund 1005 full"],
        "gold": {"status": "proposal", "order_id": "1005", "action": "refund", "amount_minor": 1800},
    }
    result = run_case(case, "improved", tmp_path / "one")
    assert result["passed"]
    assert result["receipts"][0]["amount_minor"] == 1800
    assert result["execution"]["receipt"] == result["receipts"][0]
    assert any(e["kind"] == "business_committed" for e in result["audit"])
    assert summarize([result])["real_model_verified"] is False


def test_baseline_does_not_get_a_less_safe_executor(tmp_path):
    case = {
        "id": "sample",
        "family": "missing",
        "split": "dev",
        "turns": ["refund 1005"],
        "gold": {"status": "clarify", "no_commit": True},
    }
    baseline = run_case(case, "baseline", tmp_path / "baseline")
    improved = run_case(case, "improved", tmp_path / "improved")
    # Shared guard now vetoes upstream's inferred maximum, requiring clarification.
    assert baseline["passed"]
    assert baseline["turns"][-1]["semantic_guard"]["code"] == "AMBIGUOUS_REFUND_AMOUNT"
    assert improved["passed"]
    assert baseline["receipts"] == improved["receipts"] == []
