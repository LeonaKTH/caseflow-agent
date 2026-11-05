"""Read-only source-tree release check. Run before tests create local artifacts.

This pattern scan is not a guarantee that every form of sensitive data is absent.
The private release review additionally checks configured-key equality and withheld
task contents. No Git history, credentials or private corpus is required here.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# One deliberate redaction regression fixture. Exact path + value hash only.
SYNTHETIC_TEST_VALUE_SHA256 = "7db31c8add093a0ed723e9d032dac17d75b9189dfd2055a80ea9dc2d0dd3b0dc"


def inspect(root=ROOT, allow_git=False):
    forbidden = []
    findings = []
    known_synthetic = 0
    secret = re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
    )
    host_path = re.compile(
        r"(?:(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s\"'<>]+|/(?:Users|home)/[^\s\"'<>]+|(?:LAPTOP|DESKTOP)-[A-Z0-9]{4,})",
        re.I,
    )
    private_modules = {
        "independent_eval.py",
        "independent_run.py",
        "test_independent_eval.py",
        "test_independent_run.py",
        "smoke.py",
        "test_smoke_session.py",
    }
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.relative_to(root).parts[0] == ".git":
            if not allow_git and rel == ".git":
                forbidden.append(".git/")
            continue
        if path.is_dir():
            if path.name in {
                ".git",
                ".local",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                ".ruff_cache",
                "build",
                "dist",
            } or path.name.endswith(".egg-info"):
                forbidden.append(rel + "/")
            continue
        if not path.is_file():
            forbidden.append(rel)
            continue
        if (
            (path.name.startswith(".env") and path.name != ".env.example")
            or path.name in private_modules
            or path.name.startswith("independent_eval_v2")
            or path.suffix.lower()
            in {
                ".db",
                ".sqlite",
                ".sqlite3",
                ".dpapi",
                ".pem",
                ".pfx",
                ".pyc",
                ".pyo",
                ".log",
                ".xml",
                ".pt",
                ".pth",
                ".safetensors",
            }
            or ".db-" in path.name
            or path.stat().st_size > 10_000_000
        ):
            forbidden.append(rel)
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            findings.append({"file": rel, "kind": "unexpected_binary"})
            continue
        if host_path.search(text):
            findings.append({"file": rel, "kind": "host_metadata"})
        for match in secret.finditer(text):
            if (
                rel == "tests/caseflow/test_web_runtime.py"
                and hashlib.sha256(match.group().encode()).hexdigest() == SYNTHETIC_TEST_VALUE_SHA256
            ):
                known_synthetic += 1
            else:
                findings.append({"file": rel, "kind": "credential_pattern"})
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text):
            findings.append({"file": rel, "kind": "private_key"})
    return {
        "forbidden": sorted(set(forbidden)),
        "findings": findings,
        "intentional_synthetic_redaction_fixtures": known_synthetic,
        "passed": not forbidden and not findings,
        "provider_requests": 0,
        "git_metadata_present": (root / ".git").exists(),
        "git_history_audited": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-git", action="store_true", help="Allow a new checkout's Git metadata; never audit/copy it"
    )
    args = parser.parse_args()
    report = inspect(allow_git=args.allow_git)
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
