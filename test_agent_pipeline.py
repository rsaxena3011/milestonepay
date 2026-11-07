#!/usr/bin/env python3
"""Local tests for the MilestonePay agent (agent/ package).

No PayPal calls, no live model calls, no secrets. Covers:
  1. Deterministic extraction on the clean fixture: every field,
     confidences, and source quotes that really are in the text.
  2. Missing-amount fixture: amount stays None and is flagged
     BLOCKING; an approved draft still cannot invoice.
  3. Ambiguous-deliverable fixture: ambiguity flags raised, amount
     and NET_15 terms still extracted.
  4. Approval gate: an UNAPPROVED draft cannot produce an invoice
     call (fake client records zero calls); a REJECTED draft too.
  5. Approved path end to end against the fake client: payload
     matches the live_session_api.cmd_invoice shape; approval is
     recorded on the draft AND in the audit log.
  6. LLM adapter: fails closed with a clear error when
     unconfigured; with an injected fake transport (no live call)
     it parses structured output, keeps a missing amount None, and
     caps confidence on an ungrounded source quote.
Exit 0 only if every check passes.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent.approval import approve_draft, reject_draft
from agent.extractors import (
    DeterministicExtractor, LLMExtractor, ProviderNotConfigured,
    load_fixture)
from agent.handoff import (
    ApprovalRequiredError, FakeInvoiceClient, create_invoice_for_draft)

AUDIT = "/tmp/c016_agent_audit_test.jsonl"
if os.path.exists(AUDIT):
    os.remove(AUDIT)

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name} {detail}")
    if not cond:
        failures.append(name)


ext = DeterministicExtractor()

# 1. Clean fixture: full extraction with receipts.
clean = load_fixture("evidence_clean.txt")
d = ext.extract(clean, evidence_source="fixture:evidence_clean.txt")
check("clean: client", d.fields["client"].value == "Meridian Studios",
      str(d.fields["client"].value))
check("clean: email",
      d.fields["client_email"].value == "accounts@meridianstudios.example")
check("clean: title",
      d.fields["milestone_title"].value == "Homepage redesign and launch",
      str(d.fields["milestone_title"].value))
check("clean: 3 deliverables",
      len(d.fields["deliverables"].value or []) == 3,
      str(d.fields["deliverables"].value))
check("clean: amount 2500.0", d.fields["amount"].value == 2500.0,
      str(d.fields["amount"].value))
check("clean: currency USD", d.fields["currency"].value == "USD")
check("clean: terms NET_30",
      (d.fields["due_terms"].value or {}).get("term_type") == "NET_30",
      str(d.fields["due_terms"].value))
check("clean: no blocking flags", d.blocking_flags() == [],
      str(d.blocking_flags()))
check("clean: confidences positive",
      all(d.fields[n].confidence > 0 for n in
          ("client", "milestone_title", "amount", "currency")))
check("clean: quotes are real substrings",
      all(d.fields[n].source_quote in clean for n in
          ("client", "milestone_title", "amount")))

# 2. Missing amount: None, flagged, and uninvoiceable even approved.
missing = load_fixture("evidence_missing_amount.txt")
d2 = ext.extract(missing,
                 evidence_source="fixture:evidence_missing_amount.txt")
check("missing: amount is None", d2.fields["amount"].value is None)
check("missing: amount BLOCKING flag",
      any("amount not stated" in f for f in d2.blocking_flags()),
      str(d2.blocking_flags()))
check("missing: client inferred (low conf)",
      d2.fields["client"].value == "Kavya Retail Group"
      and d2.fields["client"].confidence <= 0.6,
      str(d2.fields["client"].value))
check("missing: title + deliverables still extracted",
      d2.fields["milestone_title"].value == "Brand shoot delivery"
      and len(d2.fields["deliverables"].value or []) == 2)
fake = FakeInvoiceClient()
approve_draft(d2, "test-approver", audit_path=AUDIT)
try:
    create_invoice_for_draft(d2, fake)
    blocked = False
except ValueError:
    blocked = True
check("missing: approved-but-incomplete draft blocked", blocked)
check("missing: fake client never invoked", fake.calls == [])

# 3. Ambiguous deliverable: flagged, rest extracted.
amb = load_fixture("evidence_ambiguous.txt")
d3 = ext.extract(amb, evidence_source="fixture:evidence_ambiguous.txt")
check("ambiguous: two ambiguity flags",
      sum(1 for f in d3.flags if f.startswith("Ambiguous")) == 2,
      str(d3.flags))
check("ambiguous: amount 1800 USD from $",
      d3.fields["amount"].value == 1800.0
      and d3.fields["currency"].value == "USD",
      str(d3.fields["amount"].value))
check("ambiguous: 'within 15 days' -> NET_15",
      (d3.fields["due_terms"].value or {}).get("term_type") == "NET_15",
      str(d3.fields["due_terms"].value))
check("ambiguous: no blocking flags", d3.blocking_flags() == [],
      str(d3.blocking_flags()))

# 4. Approval gate: unapproved and rejected drafts cannot invoice.
d4 = ext.extract(clean, evidence_source="fixture:evidence_clean.txt")
fake4 = FakeInvoiceClient()
try:
    create_invoice_for_draft(d4, fake4)
    refused = False
except ApprovalRequiredError:
    refused = True
check("gate: unapproved draft refused", refused)
check("gate: fake client never invoked", fake4.calls == [])
reject_draft(d4, "test-approver", audit_path=AUDIT)
try:
    create_invoice_for_draft(d4, fake4)
    refused_rejected = False
except ApprovalRequiredError:
    refused_rejected = True
check("gate: rejected draft refused", refused_rejected)
check("gate: fake client still never invoked", fake4.calls == [])

# 5. Approved path end to end + payload shape + audit records.
d5 = ext.extract(clean, evidence_source="fixture:evidence_clean.txt")
fake5 = FakeInvoiceClient()
rec = approve_draft(d5, "test-approver", audit_path=AUDIT,
                    note="unit test approval")
check("approved: status + approval recorded on draft",
      d5.status == "APPROVED" and d5.approval["by"] == "test-approver")
result = create_invoice_for_draft(d5, fake5)
check("approved: exactly one fake invoice call", len(fake5.calls) == 1)
payload = fake5.calls[0]
check("approved: payload has cmd_invoice shape",
      set(payload) == {"detail", "invoicer_info",
                       "primary_recipients", "items"},
      str(sorted(payload)))
item = payload["items"][0]
check("approved: line item amount '2500.00' USD",
      item["unit_amount"] == {"currency_code": "USD",
                              "value": "2500.00"},
      str(item["unit_amount"]))
check("approved: payment term NET_30",
      payload["detail"]["payment_term"] == {"term_type": "NET_30"})
check("approved: recipient email carried",
      payload["primary_recipients"][0]["billing_info"].get(
          "email_address") == "accounts@meridianstudios.example")
check("approved: result is a DRAFT id",
      result["status"] == "DRAFT" and result["id"].startswith("FAKE-INV-"))

# 6. LLM adapter: fail-closed unconfigured; fake-transport parsing.
try:
    LLMExtractor(env={}).extract("anything")
    llm_closed = False
except ProviderNotConfigured as exc:
    llm_closed = "MILESTONEPAY_AI_API_KEY" in str(exc)
check("llm: unconfigured fails closed, names the variables", llm_closed)

canned = {"choices": [{"message": {"content": json.dumps({
    "client": {"value": "Meridian Studios", "confidence": 0.9,
               "source_quote": "Client: Meridian Studios"},
    "client_email": {"value": None, "confidence": 0.0,
                     "source_quote": None},
    "milestone_title": {"value": "Homepage redesign and launch",
                        "confidence": 0.9,
                        "source_quote": "Milestone 2: Homepage "
                                        "redesign and launch"},
    "deliverables": {"value": ["Invented deliverable"],
                     "confidence": 0.9,
                     "source_quote": "this quote is not in the text"},
    "amount": {"value": None, "confidence": 0.0, "source_quote": None},
    "currency": {"value": None, "confidence": 0.0,
                 "source_quote": None},
    "due_terms": {"value": {"term_type": "NET_30"}, "confidence": 0.9,
                  "source_quote": "Payment terms: Net 30"},
})}}]}
seen = {}


def fake_transport(url, headers, payload):
    seen["url"] = url
    seen["model"] = payload.get("model")
    return canned


llm = LLMExtractor(env={"MILESTONEPAY_AI_PROVIDER": "test",
                        "MILESTONEPAY_AI_MODEL": "test-model",
                        "MILESTONEPAY_AI_API_KEY": "not-a-real-key"},
                   transport=fake_transport)
d6 = llm.extract(clean, evidence_source="fixture:evidence_clean.txt")
check("llm: transport received configured model",
      seen.get("model") == "test-model", str(seen.get("model")))
check("llm: grounded fields kept",
      d6.fields["client"].value == "Meridian Studios"
      and d6.fields["client"].confidence == 0.9)
check("llm: omitted amount stays None + BLOCKING",
      d6.fields["amount"].value is None
      and any("amount not stated" in f for f in d6.blocking_flags()))
check("llm: ungrounded quote capped + flagged",
      d6.fields["deliverables"].confidence <= 0.2
      and any("unverified source quote" in f for f in d6.flags),
      str(d6.fields["deliverables"].confidence))

# Audit log: approval + rejection lines, JSON, ts-stamped.
lines = [json.loads(l) for l in open(AUDIT).read().strip().split("\n")]
actions = [l["action"] for l in lines]
check("audit: approve/reject lines present",
      actions.count("draft_approved") == 2
      and actions.count("draft_rejected") == 1, str(actions))
check("audit: every line ts-stamped",
      all(isinstance(l.get("ts"), float) for l in lines))
check("audit: approval line carries draft id + approver",
      any(l["action"] == "draft_approved"
          and l["draft_id"] == d5.draft_id
          and l["actor"] == "test-approver" for l in lines))

print(f"\n{'ALL TESTS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
