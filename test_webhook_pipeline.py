#!/usr/bin/env python3
"""Local tests for webhook_listener.py. No PayPal calls, no secrets.

Covers the four preflight proof points:
  1. Bad-signature event rejected, ledger untouched.
  2. Replay of the same event id changes the ledger exactly once.
  3. Ledger follows the canonical fetch, not the payload (payload
     claims PAID in full; canonical says PARTIALLY_PAID / 40 paid).
  4. Partial payment records PARTIAL state with remaining 60.00.
Plus a live localhost HTTP round-trip through the real listener.
Exit 0 only if every check passes.
"""

import json
import os
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webhook_listener import Pipeline, demo_fetch, demo_verify, make_handler
from http.server import HTTPServer

AUDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "webhook_audit_test.jsonl")
if os.path.exists(AUDIT):
    os.remove(AUDIT)

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name} {detail}")
    if not cond:
        failures.append(name)


p = Pipeline(demo_verify, demo_fetch, audit_path=AUDIT)

# 1. Bad signature rejected, ledger untouched.
out = p.handle({"id": "EV-BAD-1", "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"id": "INV-DEMO-FULL"}})
check("bad signature rejected", out["outcome"] == "rejected_bad_signature")
check("ledger empty after rejection", p.ledger.invoices == {})

# 2+3+4. Full payment, then replay, then a payload that lies.
out = p.handle({"id": "EV-OK-1", "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"id": "INV-DEMO-FULL"}})
check("full payment applied", out["outcome"] == "applied"
      and out["ledger"] == {"status": "PAID", "amount_due": 100.0,
                            "amount_paid": 100.0, "remaining": 0.0})
out = p.handle({"id": "EV-OK-1", "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"id": "INV-DEMO-FULL"}})
check("replay ignored", out["outcome"] == "duplicate_ignored")

# Payload claims full payment for the partial invoice; canonical wins.
out = p.handle({"id": "EV-OK-2", "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"id": "INV-DEMO-PARTIAL"}})
check("partial recorded from canonical, not payload",
      out["ledger"] == {"status": "PARTIALLY_PAID", "amount_due": 100.0,
                        "amount_paid": 40.0, "remaining": 60.0},
      str(out["ledger"]))

# 5. Localhost HTTP round-trip through the real handler.
server = HTTPServer(("127.0.0.1", 0), make_handler(p))
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/webhook",
    data=json.dumps({"id": "EV-OK-3", "event_type": "INVOICING.INVOICE.PAID",
                     "resource": {"id": "INV-DEMO-FULL"}}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=10) as resp:
    http_out = json.loads(resp.read())
check("localhost HTTP round-trip applied", http_out["outcome"] == "applied")

# 5b. Non-object JSON body is rejected at the handler, not crashed on.
import urllib.error
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/webhook",
    data=json.dumps(["not", "an", "object"]).encode(),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=10)
    nondict_status = 200
except urllib.error.HTTPError as exc:
    nondict_status = exc.code
check("non-object JSON body gets HTTP 400", nondict_status == 400,
      f"got {nondict_status}")
server.shutdown()

# 6. Real PayPal shape: invoice id nested at resource.invoice.id
# (the exact shape that broke the live session on 2026-11-05).
out = p.handle({"id": "EV-OK-4", "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"invoice": {"id": "INV-DEMO-PARTIAL"}}})
check("nested resource.invoice shape applied",
      out["outcome"] == "applied"
      and out["invoice_id"] == "INV-DEMO-PARTIAL", str(out.get("outcome")))

# 7. Canonical fetch failure: event must NOT be marked seen, so its
# redelivery applies instead of being duplicate_ignored (round-1 fix).
flaky_calls = {"n": 0}


def flaky_fetch(invoice_id):
    flaky_calls["n"] += 1
    if flaky_calls["n"] == 1:
        raise RuntimeError("simulated canonical GET failure")
    return demo_fetch(invoice_id)


p2 = Pipeline(demo_verify, flaky_fetch, audit_path=AUDIT)
evt = {"id": "EV-OK-5", "event_type": "INVOICING.INVOICE.PAID",
       "resource": {"id": "INV-DEMO-FULL"}}
out = p2.handle(evt)
check("fetch failure audited as fetch_failed",
      out["outcome"] == "fetch_failed", str(out.get("outcome")))
check("ledger untouched after fetch failure", p2.ledger.invoices == {})
out = p2.handle(evt)
check("redelivery after fetch failure applies",
      out["outcome"] == "applied"
      and out["ledger"]["status"] == "PAID", str(out.get("outcome")))

# 8. Non-dict resource field: rejected_malformed, never a crash
# (round-2 find: this raised AttributeError before the fix).
out = p.handle({"id": "EV-OK-6", "event_type": "INVOICING.INVOICE.PAID",
                "resource": "INV-DEMO-FULL"})
check("non-dict resource rejected_malformed",
      out["outcome"] == "rejected_malformed", str(out.get("outcome")))

audit_lines = open(AUDIT).read().strip().split("\n")
check("audit log has one line per outcome", len(audit_lines) == 9,
      f"got {len(audit_lines)}")
check("audit lines are valid JSON",
      all(json.loads(line).get("outcome") for line in audit_lines))

print(f"\n{'ALL TESTS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
