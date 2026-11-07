#!/usr/bin/env python3
"""MilestonePay agent demo — LOCAL ONLY, fake invoice client.

Walks sample milestone evidence through the whole AI half:

  extract -> show draft (fields, confidences, flags)
          -> hand-off attempt BEFORE approval (must refuse)
          -> human approval (recorded in agent_audit.jsonl)
          -> hand-off: approved draft -> invoice DRAFT created
             against FakeInvoiceClient (no PayPal contact)

Run from the c016-paypal folder:
  python3 -m agent.demo                 # all three bundled fixtures
  python3 -m agent.demo evidence_clean.txt
  python3 -m agent.demo /path/to/your-evidence.txt

The extractor used is the deterministic STAND-IN provider (no
model). The LLM provider runs only when configured via
MILESTONEPAY_AI_* environment variables AND --llm is passed; this
demo never calls it implicitly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from agent.approval import approve_draft          # noqa: E402
from agent.extractors import (                     # noqa: E402
    DeterministicExtractor, LLMExtractor, FIXTURE_NAMES, load_fixture)
from agent.handoff import (                        # noqa: E402
    ApprovalRequiredError, FakeInvoiceClient, create_invoice_for_draft)


def show_draft(draft):
    print("  draft %s via %s" % (draft.draft_id, draft.provider_name))
    for name in ("client", "client_email", "milestone_title",
                 "deliverables", "amount", "currency", "due_terms"):
        fx = draft.fields[name]
        print("    %-16s %-40s conf=%.2f  %s"
              % (name, repr(fx.value)[:40], fx.confidence, fx.note))
    if draft.flags:
        print("  FLAGS:")
        for flag in draft.flags:
            print("    ! " + flag)
    else:
        print("  FLAGS: none")


def walk(text, source, extractor, approver="demo-operator"):
    print("=" * 68)
    print("EVIDENCE (%s):" % source)
    print(text.strip()[:600])
    print("-" * 68)
    draft = extractor.extract(text, evidence_source=source)
    print("STAGE 1 — EXTRACTION")
    show_draft(draft)
    client = FakeInvoiceClient()
    print("STAGE 2 — HAND-OFF BEFORE APPROVAL (must refuse)")
    try:
        create_invoice_for_draft(draft, client)
        print("  !! NOT REFUSED — gate failure")
    except ApprovalRequiredError as exc:
        print("  refused, as required: %s" % exc)
    print("  fake client calls so far: %d" % len(client.calls))
    print("STAGE 3 — HUMAN APPROVAL")
    approve_draft(draft, approver,
                  note="demo approval of a sample draft")
    print("  approved by %s; audit line written to agent_audit.jsonl"
          % approver)
    print("STAGE 4 — HAND-OFF AFTER APPROVAL")
    try:
        result = create_invoice_for_draft(draft, client)
        print("  invoice DRAFT created via %s: %s (status %s)"
              % (client.name, result["id"], result["status"]))
        item = result["payload"]["items"][0]
        print("  line item: %s — %s %s"
              % (item["name"], item["unit_amount"]["currency_code"],
                 item["unit_amount"]["value"]))
    except ValueError as exc:
        print("  blocked by validation, no invoice created: %s" % exc)
    print("  fake client calls total: %d" % len(client.calls))
    print()


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if "--llm" in argv:
        extractor = LLMExtractor()
        print("provider: LLM adapter (env-configured)")
    else:
        extractor = DeterministicExtractor()
        print("provider: %s" % extractor.name)
    if args:
        for arg in args:
            if os.path.exists(arg):
                with open(arg, "r", encoding="utf-8") as fh:
                    walk(fh.read(), arg, extractor)
            else:
                walk(load_fixture(arg), "fixture:" + arg, extractor)
    else:
        for name in FIXTURE_NAMES:
            walk(load_fixture(name), "fixture:" + name, extractor)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
