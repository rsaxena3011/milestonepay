"""Hand-off: approved MilestoneDraft -> PayPal invoice DRAFT.

The gate lives here, not just in the caller's discipline:
create_invoice_for_draft() refuses, in order —
  1. any draft whose status is not APPROVED (ApprovalRequiredError),
  2. any approved draft that still carries BLOCKING flags
     (ValueError listing them: missing amount, currency, client,
     title, or deliverables — deterministic validation before any
     API call, per MERGED_PLAN_2026-10-30.md).
Only then is the client invoked.

build_invoice_payload() produces the same payload shape as the
proven live-session code (live_session_api.cmd_invoice): detail /
invoicer_info / primary_recipients / items, one line item for the
milestone at the extracted amount.

Clients:
  FakeInvoiceClient — records payloads, returns fake ids. Used by
  the demo and the tests. Labelled fake everywhere it appears.
  PayPalInvoiceClient — the real sandbox path. It reuses the
  existing client code: paypal_webhook_verifier (load_env,
  assert_sandbox, get_access_token, BASE) and live_session_api
  (auth_headers, extract_id_from_href). It creates a DRAFT invoice
  only — it never sends. Sending stays a separate, human-driven
  live-session step, as in the proven 2026-11-02/11-05 sessions.
  It is NOT exercised in the 2026-11-05 build run (no live calls
  were made); wiring correctness rests on sharing the proven code
  path and payload shape, and must be smoke-tested in the next
  live sandbox session before any filing claim relies on it.
"""

import uuid


class ApprovalRequiredError(RuntimeError):
    """Raised when an unapproved (or rejected) draft is handed off."""


def _recipient_name(client):
    parts = str(client).split()
    if len(parts) == 1:
        return {"given_name": parts[0], "surname": parts[0]}
    return {"given_name": " ".join(parts[:-1]), "surname": parts[-1]}


def build_invoice_payload(draft):
    """cmd_invoice-shaped payload for an approved, complete draft."""
    f = draft.fields
    amount = f["amount"].value
    currency = f["currency"].value
    terms = f["due_terms"].value or {}
    term_type = terms.get("term_type") or "DUE_ON_RECEIPT"
    payment_term = {"term_type": term_type}
    if term_type == "DUE_ON_DATE_SPECIFIED" and terms.get("due_date"):
        payment_term["due_date"] = terms["due_date"]
    deliverables = f["deliverables"].value or []
    note = ("MilestonePay draft %s (provider: %s). Deliverables: %s"
            % (draft.draft_id, draft.provider_name,
               "; ".join(deliverables)))
    billing = {"name": _recipient_name(f["client"].value)}
    if f["client_email"].present:
        billing["email_address"] = f["client_email"].value
    return {
        "detail": {
            "invoice_number": "C016-AGENT-" + draft.draft_id[-8:],
            "currency_code": currency,
            "note": note,
            "payment_term": payment_term,
        },
        "invoicer_info": {
            "name": {"given_name": "MilestonePay", "surname": "Sandbox"},
        },
        "primary_recipients": [{"billing_info": billing}],
        "items": [
            {
                "name": str(f["milestone_title"].value)[:127],
                "quantity": "1",
                "unit_amount": {
                    "currency_code": currency,
                    "value": "%.2f" % float(amount),
                },
            }
        ],
    }


class FakeInvoiceClient:
    """Test/demo double. Records every payload; creates nothing real."""

    name = "fake (no PayPal contact)"

    def __init__(self):
        self.calls = []

    def create_draft_invoice(self, payload):
        self.calls.append(payload)
        return {"id": "FAKE-INV-%04d" % len(self.calls),
                "status": "DRAFT", "payload": payload}


class PayPalInvoiceClient:
    """Real sandbox client reusing the proven session code path.

    Construction performs the sandbox assertion and OAuth token
    fetch (in memory only; the token is never printed or stored).
    create_draft_invoice POSTs /v2/invoicing/invoices — a DRAFT;
    it never sends the invoice.
    """

    name = "paypal-sandbox"

    def __init__(self, session=None, env=None):
        import sys, os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if here not in sys.path:
            sys.path.insert(0, here)
        from paypal_webhook_verifier import (
            BASE, assert_sandbox, get_access_token, load_env)
        from live_session_api import auth_headers, extract_id_from_href
        self._base = BASE
        self._auth_headers = auth_headers
        self._extract_id = extract_id_from_href
        env = load_env() if env is None else env
        if not assert_sandbox(env):
            raise RuntimeError(
                "ABORT: PAYPAL_ENV is not 'sandbox' or base is not "
                "the sandbox API.")
        if session is None:
            from live_session_api import make_session
            session = make_session()
        self._session = session
        self._token, _scopes = get_access_token(session, env)

    def create_draft_invoice(self, payload):
        suffix = uuid.uuid4().hex[:8]
        resp = self._session.post(
            self._base + "/v2/invoicing/invoices",
            headers=self._auth_headers(
                self._token, "C016-AGENT-CREATE-" + suffix),
            json=payload, timeout=30, allow_redirects=False)
        if resp.status_code != 201:
            raise RuntimeError(
                "draft invoice creation failed: HTTP %d"
                % resp.status_code)
        body = resp.json()
        invoice_id = body.get("id", "") or \
            self._extract_id(body.get("href", ""))
        if not invoice_id:
            raise RuntimeError("draft response had no invoice id")
        return {"id": invoice_id, "status": "DRAFT"}


def create_invoice_for_draft(draft, client):
    """The gated hand-off. See the module docstring for the rules."""
    if draft.status != "APPROVED":
        raise ApprovalRequiredError(
            "draft %s is %s, not APPROVED; no invoice can be "
            "created. A human must approve it first "
            "(approval.approve_draft)." % (draft.draft_id, draft.status))
    blocking = draft.blocking_flags()
    if blocking:
        raise ValueError(
            "draft %s is approved but has blocking gaps; no invoice "
            "created: %s" % (draft.draft_id, " | ".join(blocking)))
    payload = build_invoice_payload(draft)
    return client.create_draft_invoice(payload)
