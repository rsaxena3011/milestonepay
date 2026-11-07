#!/usr/bin/env python3
"""C016 MilestonePay: webhook listener + reconciliation pipeline, LOCAL ONLY.

Built 2026-11-02 for the Nov 2 preflight item "Buyer-side payment +
webhook end-to-end proof" (PREFLIGHT_CHECKLIST_2026-10-30.md).

What this proves locally, without any PayPal call, account, or secret:
  1. Signature gate: an event is trusted only if the injected verifier
     accepts it; a rejected event is logged and never touches the ledger.
  2. Event-ID dedupe: replaying the same event id changes the ledger
     exactly once.
  3. Canonical state fetch: after a payment event the ledger is updated
     from the injected canonical fetcher (which, in production, is
     GET /v2/invoicing/invoices/{id}), NEVER from the webhook payload.
  4. Partial payment: a PARTIALLY_PAID canonical state records PARTIAL
     with the correct remaining balance, even though
     INVOICING.INVOICE.PAID can fire for partial payments.

What this does NOT prove (explicitly deferred to the tunnel stage):
  - The real PayPal signature verifier (POST
    /v1/notifications/verify-webhook-signature) -- the production
    verifier now lives in paypal_webhook_verifier.py and plugs into
    Pipeline(verify=...) with the delivery headers the handler forwards;
    the local tests here use a fake verifier so pipeline logic, not
    PayPal's API, is what they test. See WEBHOOK_PROOF_PREP_2026-11-04.md
    for how far the real verifier was exercised against the sandbox.
  - Real webhook registration (POST /v1/notifications/webhooks) and
    delivery over a public URL (local tunnel or hosted endpoint).
  - The sandbox buyer-side payment itself (that step needs the
    sandbox personal account and is NOT simulated as a real payment).

No credentials are read. No network call is made except the local
HTTP listener itself. Audit log is JSONL, one line per event outcome.
Run the tests with: python3 test_webhook_pipeline.py
Run the listener with: python3 webhook_listener.py [port]  (default 8766)
  POST /webhook with a JSON body:
    {"id": "<event id>", "event_type": "INVOICING.INVOICE.PAID",
     "resource": {"id": "<invoice id>"}}
  The listener uses the demo verifier/fetcher below; for a real run,
  replace demo_verify / demo_fetch with the PayPal-backed versions.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY_BYTES = 1_000_000

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_PATH = os.path.join(HERE, "webhook_audit.jsonl")


class Ledger:
    """Invoice id -> {status, amount_due, amount_paid, remaining}."""

    def __init__(self):
        self.invoices = {}

    def apply_canonical(self, invoice_id, canonical):
        """Update ONLY from canonical state. Returns True if changed."""
        status = canonical["status"]
        due = canonical["amount_due"]
        paid = canonical["amount_paid"]
        remaining = round(due - paid, 2)
        new = {
            "status": status,
            "amount_due": due,
            "amount_paid": paid,
            "remaining": remaining,
        }
        old = self.invoices.get(invoice_id)
        if old == new:
            return False
        self.invoices[invoice_id] = new
        return True


class Pipeline:
    def __init__(self, verify, fetch, audit_path=AUDIT_PATH):
        self.verify = verify
        self.fetch = fetch
        self.ledger = Ledger()
        self.seen_event_ids = set()
        self.audit_path = audit_path

    def _audit(self, record):
        # ts added 2026-11-05 (review round 1): audit lines previously
        # carried no timestamp, so a line could not stand as evidence
        # on its own. Additive only; older lines simply lack the field.
        record = {"ts": round(time.time(), 3), **record}
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def handle(self, event, headers=None):
        """Process one webhook event dict. Returns an outcome dict.

        `headers` carries the delivery's HTTP headers (the handler passes
        the PAYPAL-* transmission headers through): the production
        verifier in paypal_webhook_verifier.py needs them to call
        POST /v1/notifications/verify-webhook-signature. Local demo
        verifiers ignore them.
        """
        event_id = event.get("id")
        event_type = event.get("event_type")
        # Real PayPal invoicing events (proven live 2026-11-05) nest the
        # invoice object at resource.invoice; the local demo shape put
        # the id directly at resource.id. Accept both.
        # A non-dict resource (string/list from a malformed or forged
        # delivery) must fall through to rejected_malformed, not crash
        # on .get (round-2 fix, 2026-11-05).
        resource = event.get("resource")
        if not isinstance(resource, dict):
            resource = {}
        invoice_id = resource.get("id")
        if not invoice_id and isinstance(resource.get("invoice"), dict):
            invoice_id = resource["invoice"].get("id")
        if not event_id or not event_type or not invoice_id:
            # Diagnostics added 2026-11-05 during the live proof: real
            # PayPal deliveries were landing as rejected_malformed, so
            # record the payload's key SHAPE (names only, no values
            # beyond resource_type) to find where the invoice id lives.
            resource = event.get("resource")
            outcome = {
                "outcome": "rejected_malformed",
                "event_id": event_id,
                "event_keys": sorted(event.keys()),
                "resource_type": event.get("resource_type"),
                "resource_keys": sorted(resource.keys())
                if isinstance(resource, dict) else None,
            }
            self._audit({**outcome, "event_type": event_type})
            return outcome
        if event_id in self.seen_event_ids:
            outcome = {"outcome": "duplicate_ignored", "event_id": event_id}
            self._audit({**outcome, "event_type": event_type})
            return outcome
        if not self.verify(event, headers):
            outcome = {"outcome": "rejected_bad_signature", "event_id": event_id}
            self._audit({**outcome, "event_type": event_type})
            return outcome
        # Fetch + apply BEFORE marking the event seen (fixed 2026-11-05,
        # review round 1): the production fetcher raises on any PayPal
        # error, and the old order marked the id seen first, so one
        # transient canonical-GET failure permanently dropped the event
        # (its redelivery was then duplicate_ignored, never applied,
        # with no audit trace of the loss). A fetch failure now audits
        # fetch_failed, leaves the id unseen, and the handler answers
        # 503 so PayPal redelivers.
        try:
            canonical = self.fetch(invoice_id)
            changed = self.ledger.apply_canonical(invoice_id, canonical)
        except Exception as exc:
            outcome = {
                "outcome": "fetch_failed",
                "event_id": event_id,
                "invoice_id": invoice_id,
                "error_class": type(exc).__name__,
            }
            self._audit({**outcome, "event_type": event_type})
            return outcome
        self.seen_event_ids.add(event_id)
        outcome = {
            "outcome": "applied",
            "event_id": event_id,
            "event_type": event_type,
            "invoice_id": invoice_id,
            "canonical_status": canonical["status"],
            "ledger_changed": changed,
            "ledger": self.ledger.invoices[invoice_id],
        }
        self._audit(outcome)
        return outcome


# --- Demo injectables (local tests only, NOT PayPal) -----------------------
def demo_verify(event, headers=None):
    """Fake verifier: accepts events whose id starts with 'EV-OK-'."""
    return str(event.get("id", "")).startswith("EV-OK-")


DEMO_CANONICAL = {
    "INV-DEMO-FULL": {"status": "PAID", "amount_due": 100.00, "amount_paid": 100.00},
    "INV-DEMO-PARTIAL": {"status": "PARTIALLY_PAID", "amount_due": 100.00, "amount_paid": 40.00},
}


def demo_fetch(invoice_id):
    return DEMO_CANONICAL[invoice_id]


def make_handler(pipeline):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/webhook":
                self.send_response(404)
                self.end_headers()
                return
            # Hardened 2026-11-05 (review round 1): a non-numeric or
            # negative Content-Length used to raise inside int()/read()
            # and drop the connection with no response and no audit
            # line; an unbounded length could make the handler read
            # indefinitely; and a valid-JSON non-object body (list,
            # string) crashed Pipeline.handle on event.get.
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = -1
            if length < 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"outcome":"rejected_malformed"}')
                return
            if length > MAX_BODY_BYTES:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"outcome":"rejected_too_large"}')
                return
            body = self.rfile.read(length)
            try:
                event = json.loads(body.decode("utf-8"))
            except Exception:
                event = None
            if not isinstance(event, dict):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"outcome":"rejected_malformed"}')
                return
            # Forward the PayPal transmission headers (lowercased names)
            # so a production verifier can call verify-webhook-signature.
            headers = {
                k.lower(): v
                for k, v in self.headers.items()
                if k.lower().startswith("paypal-")
            }
            outcome = pipeline.handle(event, headers=headers)
            payload = json.dumps(outcome).encode("utf-8")
            # Ack policy (2026-11-05): applied / duplicate / malformed
            # are final, answer 200. rejected_bad_signature and
            # fetch_failed may be TRANSIENT (expired OAuth token makes
            # the verifier fail closed; a canonical GET can fail
            # mid-flight), so answer 503 and let PayPal redeliver
            # instead of silently dropping a real event. A genuinely
            # forged event simply stays rejected on every retry.
            status = 503 if outcome.get("outcome") in (
                "rejected_bad_signature", "fetch_failed") else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    pipeline = Pipeline(demo_verify, demo_fetch)
    server = HTTPServer(("127.0.0.1", port), make_handler(pipeline))
    print(f"MilestonePay webhook listener on 127.0.0.1:{port} (local only)")
    print(f"Audit log: {AUDIT_PATH}")
    server.serve_forever()
