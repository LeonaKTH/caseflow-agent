"""Interactive chat in the terminal: python -m support_agent [--customer C100]

Refunds that need approval are shown to you as the reviewer, so you can try both sides.
"""

from __future__ import annotations

import argparse

from support_agent.models import get_model
from support_agent.service import SupportService


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the support agent.")
    parser.add_argument("--customer", default="C100", help="customer id (C100 or C200 in the sample data)")
    parser.add_argument("--provider", help="offline | openai | azure")
    args = parser.parse_args()

    service = SupportService(model=get_model(args.provider))
    name = service.store.customers[args.customer]["name"]
    print(
        f"Chatting as {name} ({args.customer}). Try: 'Where is order 1002?' or 'Refund order 1001, it arrived broken'."
    )
    print("Type 'quit' to exit.\n")
    thread_id = None
    while (message := input("you > ").strip()).lower() not in {"quit", "exit"}:
        if not message:
            continue
        result = service.chat(args.customer, message, thread_id)
        thread_id = result.thread_id
        print(f"bot > {result.reply}   [{', '.join(result.tools_used) or 'no tools'}]")
        if result.status == "awaiting_approval":
            answer = input(f"      reviewer: approve refund of {result.approval['amount']}? [y/n] ").strip().lower()
            result = service.resolve_approval(thread_id, answer.startswith("y"), reviewer="you (cli)")
            print(f"bot > {result.reply}")


if __name__ == "__main__":
    main()
