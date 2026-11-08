#!/usr/bin/env python3
"""C016 MilestonePay: sandbox probe of the REAL webhook verification path.

Run 2026-11-04 (see WEBHOOK_PROOF_PREP_2026-11-04.md). SANDBOX ONLY --
aborts unless PAYPAL_ENV is "sandbox" and the base URL is the sandbox
API. What it does, in order:

  1. Fresh client-credentials token; reports granted scopes and whether
     the webhooks / verify-webhook-signature scopes are present.
  2. GET /v1/notifications/webhooks: how many webhooks are registered
     on this sandbox app and which event types they subscribe to.
     (Webhook ids and URLs are used in memory only, never printed.)
  3. Direct call to POST /v1/notifications/verify-webhook-signature
     with FABRICATED transmission headers and a synthetic event: the
     expected, honest outcome is verification FAILURE (or a PayPal
     error) -- a SUCCESS is impossible to fabricate. This proves the
     request shape, auth, and response handling against the real API.
  4. The production verifier (make_paypal_verify) on the same fabricated
     delivery must return False (fail closed).
  5. End to end through the real HTTP handler in webhook_listener.py:
     a POST /webhook carrying fabricated PAYPAL-* headers must come
     back outcome=rejected_bad_signature, and a POST with no PAYPAL-*
     headers must also be rejected without the ledger changing.

Nothing here can produce a verification SUCCESS: that requires a real
event delivered by PayPal to a registered webhook (tunnel stage).
Credentials and the token are never printed or written anywhere; only
HTTP statuses, scope names, counts, event type names, error names and
verification_status values are printed.
"""

import base64
import json
import os
import sys
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paypal_webhook_verifier import (  # noqa: E402
    BASE, VERIFY_PATH, assert_sandbox, build_verify_payload,
    get_access_token, list_webhooks, load_env, make_paypal_verify,
)
from webhook_listener import Pipeline, demo_fetch, make_handler  # noqa: E402

WEBHOOKS_SCOPE = "https://uri.paypal.com/services/applications/webhooks"
VERIFY_SCOPE = "https://uri.paypal.com/services/applications/verify-webhook-signature"
AUDIT = os.path.join(HERE, "webhook_audit_probe_2026-11-04.jsonl")
if os.path.exists(AUDIT):
    os.remove(AUDIT)

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name} {detail}")
    if not cond:
        failures.append(name)


def fabricated_headers():
    return {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-PROBE-0000000000",
        "paypal-transmission-id": str(uuid.uuid4()),
        "paypal-transmission-sig": base64.b64encode(b"c016-probe-not-a-real-signature").decode(),
        "paypal-transmission-time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


SYNTHETIC_EVENT = {
    "id": "WH-PROBE-2026-11-04",
    "event_type": "INVOICING.INVOICE.PAID",
    "resource_type": "invoice",
    "resource": {"id": "INV-DEMO-FULL"},
}


def main():
    env = load_env()
    if not assert_sandbox(env):
        print("ABORT: PAYPAL_ENV is not 'sandbox' or base is not the sandbox API.")
        return 2
    print("SANDBOX_GUARD: ok (PAYPAL_ENV=sandbox, base=api-m.sandbox.paypal.com)")

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    # 1. Token + scopes.
    try:
        token, scopes = get_access_token(session, env)
    except RuntimeError as exc:
        print("TOKEN_ERROR:", exc)
        return 1
    print("TOKEN_HTTP_STATUS: 200")
    scope_set = set(scopes.split())
    print("WEBHOOKS_SCOPE_PRESENT:", WEBHOOKS_SCOPE in scope_set)
    print("VERIFY_SCOPE_PRESENT:", VERIFY_SCOPE in scope_set)
    print("INVOICING_SCOPE_PRESENT:",
          "https://uri.paypal.com/services/invoicing" in scope_set)

    # 2. Registered webhooks on this sandbox app (read-only).
    status, body = list_webhooks(session, token)
    print("LIST_WEBHOOKS_HTTP_STATUS:", status)
    webhooks = body.get("webhooks", []) if isinstance(body, dict) else []
    print("REGISTERED_WEBHOOKS_COUNT:", len(webhooks))
    webhook_id = ""
    for wh in webhooks:
        names = [et.get("name", "") for et in wh.get("event_types", [])
                 if isinstance(et, dict)]
        print("WEBHOOK_EVENT_TYPES:", ", ".join(names) if names else "(none)")
        if not webhook_id:
            webhook_id = wh.get("id", "")
    probe_webhook_id = webhook_id or "00000000000000000"
    print("PROBE_WEBHOOK_ID_SOURCE:",
          "registered webhook on this app" if webhook_id else "dummy (none registered)")

    # 3. Direct verification call with fabricated transmission data.
    headers = fabricated_headers()
    payload = build_verify_payload(headers, probe_webhook_id, SYNTHETIC_EVENT)
    check("verify payload builds with all 7 documented fields",
          payload is not None and len(payload) == 7)
    try:
        resp = session.post(
            BASE + VERIFY_PATH,
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"},
            json=payload, timeout=30, allow_redirects=False)
    except requests.exceptions.RequestException as exc:
        print("VERIFY_TRANSPORT_ERROR:", type(exc).__name__)
        return 1
    print("VERIFY_HTTP_STATUS:", resp.status_code)
    try:
        vbody = resp.json()
    except ValueError:
        vbody = {}
    if resp.status_code == 200:
        print("VERIFY_VERIFICATION_STATUS:", vbody.get("verification_status", ""))
        check("fabricated delivery is NOT verified",
              vbody.get("verification_status") == "FAILURE")
    else:
        print("VERIFY_ERROR_NAME:", vbody.get("name", "unknown"))
        print("VERIFY_ERROR_MESSAGE:", vbody.get("message", ""))
        details = vbody.get("details", [])
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict):
                    print("VERIFY_ERROR_DETAIL:", d.get("issue", ""), "|",
                          d.get("description", ""))
        check("fabricated delivery is NOT verified (error response, no SUCCESS)",
              vbody.get("verification_status") != "SUCCESS")

    # 4. Production verifier fails closed on the fabricated delivery.
    verify = make_paypal_verify(session, token, probe_webhook_id)
    result = verify(SYNTHETIC_EVENT, headers)
    print("PRODUCTION_VERIFIER_ON_FABRICATED:", result)
    check("production verifier returns False on fabricated delivery",
          result is False)
    check("production verifier returns False with no headers",
          verify(SYNTHETIC_EVENT, None) is False)

    # 5. End to end through the real HTTP handler.
    pipeline = Pipeline(verify, demo_fetch, audit_path=AUDIT)
    server = HTTPServer(("127.0.0.1", 0), make_handler(pipeline))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(headers_dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps(SYNTHETIC_EVENT).encode(),
            headers={"Content-Type": "application/json", **headers_dict})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())

    http_headers = {k.title(): v for k, v in fabricated_headers().items()}
    out = post(http_headers)
    print("HANDLER_WITH_FABRICATED_HEADERS:", out.get("outcome", ""))
    check("handler rejects fabricated delivery via real verifier",
          out.get("outcome") == "rejected_bad_signature")
    out = post({})
    print("HANDLER_WITHOUT_HEADERS:", out.get("outcome", ""))
    check("handler rejects headerless delivery",
          out.get("outcome") == "rejected_bad_signature")
    check("ledger untouched by both rejections", pipeline.ledger.invoices == {})
    server.shutdown()

    print(f"\n{'ALL PROBE CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
