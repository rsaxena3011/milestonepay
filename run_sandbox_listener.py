#!/usr/bin/env python3
"""C016 MilestonePay: run the webhook listener with PRODUCTION injectables.

Prepared 2026-11-04 for the tunnel stage (see
WEBHOOK_PROOF_PREP_2026-11-04.md for the full ordered runbook).
SANDBOX ONLY -- aborts unless PAYPAL_ENV is "sandbox".

Usage:
  C016_WEBHOOK_ID=<id returned by webhook registration> \
      ~/workspace/kaggle-venv/bin/python run_sandbox_listener.py [port]

The webhook id is passed via the environment at run time and is never
written to disk or printed. A fresh OAuth token is fetched at startup
and kept in memory only. When it expires, the verifier fails closed
and the listener answers those deliveries HTTP 503 (ack policy in
webhook_listener.make_handler, set 2026-11-05), so PayPal redelivers
(up to 25 retries over 3 days); restart this script with a fresh
token to resume clean processing. NOTE: before 2026-11-05 the handler
answered 200 for every outcome, so the redelivery described here did
NOT happen for token-expired deliveries; that is fixed now, but any
session run before the fix could have silently dropped events whose
verification failed transiently.

Pipeline wiring: verify = PayPal verify-webhook-signature API,
fetch = canonical GET /v2/invoicing/invoices/{id}. The ledger updates
only from the canonical fetch, never from the webhook payload.
"""

import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paypal_webhook_verifier import (  # noqa: E402
    assert_sandbox, get_access_token, load_env,
    make_paypal_fetch, make_paypal_verify,
)
from webhook_listener import Pipeline, make_handler  # noqa: E402
from http.server import HTTPServer  # noqa: E402


def main():
    env = load_env()
    if not assert_sandbox(env):
        print("ABORT: PAYPAL_ENV is not 'sandbox' or base is not the sandbox API.")
        return 2
    webhook_id = os.environ.get("C016_WEBHOOK_ID", "")
    if not webhook_id:
        print("ABORT: C016_WEBHOOK_ID is not set. Register the webhook "
              "first (runbook step 3) and pass the returned id.")
        return 2
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    try:
        token, _scopes = get_access_token(session, env)
    except RuntimeError as exc:
        print("ABORT:", exc)
        return 1

    pipeline = Pipeline(
        make_paypal_verify(session, token, webhook_id),
        make_paypal_fetch(session, token),
    )
    server = HTTPServer(("127.0.0.1", port), make_handler(pipeline))
    print(f"MilestonePay PRODUCTION listener on 127.0.0.1:{port} "
          f"(sandbox, real verifier + canonical fetch)")
    print("Audit log:", pipeline.audit_path)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
