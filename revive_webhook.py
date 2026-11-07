#!/usr/bin/env python3
"""C016 MilestonePay: webhook revival helper (2026-11-05, later session).

SANDBOX ONLY -- asserts PAYPAL_ENV == "sandbox" and the sandbox API
base before any call. The webhook id and OAuth token are never
printed; the id is written only to /tmp/c016_webhook_id (mode 600)
for the listener launcher, same as live_session_api.py.

Commands:
  patch <tunnel_base_url>   GET webhooks, PATCH the registered one to
                            <tunnel_base_url>/webhook keeping the six
                            INVOICING.* event types (JSON Patch, the
                            PayPal update form). If PATCH fails,
                            DELETE it and re-register fresh.
  invoice <invoice_id>      Canonical GET; prints status + due only.
"""

import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paypal_webhook_verifier import (  # noqa: E402
    BASE, assert_sandbox, get_access_token, list_webhooks, load_env,
)
from live_session_api import EVENT_TYPES, WEBHOOK_ID_TMP  # noqa: E402


def headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }


def save_webhook_id(webhook_id):
    fd = os.open(WEBHOOK_ID_TMP, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(webhook_id)
    os.chmod(WEBHOOK_ID_TMP, 0o600)


def cmd_patch(session, token, tunnel_base):
    new_url = tunnel_base.rstrip("/") + "/webhook"
    status, body = list_webhooks(session, token)
    print("LIST_HTTP_STATUS:", status)
    if status != 200:
        return 1
    hooks = body.get("webhooks", []) if isinstance(body, dict) else []
    print("WEBHOOK_COUNT:", len(hooks))
    if hooks:
        hook = hooks[0]
        webhook_id = hook.get("id", "")
        old_url = hook.get("url", "")
        print("CURRENT_URL:", old_url)
        ops = [
            {"op": "replace", "path": "/url", "value": new_url},
            {"op": "replace", "path": "/event_types",
             "value": [{"name": n} for n in EVENT_TYPES]},
        ]
        resp = session.patch(
            BASE + "/v1/notifications/webhooks/" + webhook_id,
            headers=headers(token), json=ops, timeout=30,
            allow_redirects=False,
        )
        print("PATCH_HTTP_STATUS:", resp.status_code)
        if resp.status_code == 200:
            try:
                rbody = resp.json()
            except ValueError:
                rbody = {}
            names = [e.get("name", "") for e in rbody.get("event_types", [])
                     if isinstance(e, dict)]
            print("PATCHED_URL:", rbody.get("url", new_url))
            print("PATCHED_EVENT_TYPES:", len(names))
            save_webhook_id(webhook_id)
            print("RESULT: webhook PATCHED; id captured to listener env file.")
            return 0
        # PATCH failed: show error name/message only, then fall back.
        try:
            err = resp.json()
        except ValueError:
            err = {}
        print("PATCH_ERROR_NAME:", err.get("name", "unknown"))
        print("PATCH_ERROR_MESSAGE:", err.get("message", ""))
        dresp = session.delete(
            BASE + "/v1/notifications/webhooks/" + webhook_id,
            headers=headers(token), timeout=30, allow_redirects=False,
        )
        print("DELETE_HTTP_STATUS:", dresp.status_code)
        if dresp.status_code not in (200, 204):
            print("RESULT: PATCH failed and DELETE failed; webhook untouched.")
            return 1
    # Fresh registration (no webhook found, or fallback after DELETE).
    payload = {
        "url": new_url,
        "event_types": [{"name": n} for n in EVENT_TYPES],
    }
    resp = session.post(
        BASE + "/v1/notifications/webhooks",
        headers=headers(token), json=payload, timeout=30,
        allow_redirects=False,
    )
    print("REGISTER_HTTP_STATUS:", resp.status_code)
    if resp.status_code != 201:
        try:
            err = resp.json()
        except ValueError:
            err = {}
        print("REGISTER_ERROR_NAME:", err.get("name", "unknown"))
        print("REGISTER_ERROR_MESSAGE:", err.get("message", ""))
        return 1
    rbody = resp.json()
    webhook_id = rbody.get("id", "")
    if not webhook_id:
        print("RESULT: registration returned no webhook id.")
        return 1
    names = [e.get("name", "") for e in rbody.get("event_types", [])
             if isinstance(e, dict)]
    print("REGISTERED_URL:", rbody.get("url", new_url))
    print("REGISTERED_EVENT_TYPES:", len(names))
    save_webhook_id(webhook_id)
    print("RESULT: webhook RE-REGISTERED; id captured to listener env file.")
    return 0


def cmd_invoice(session, token, invoice_id):
    resp = session.get(
        BASE + "/v2/invoicing/invoices/" + invoice_id,
        headers=headers(token), timeout=30, allow_redirects=False,
    )
    print("INVOICE_GET_HTTP_STATUS:", resp.status_code)
    if resp.status_code != 200:
        return 1
    body = resp.json()
    due = body.get("due_amount") or {}
    amount = body.get("amount") or {}
    print("INVOICE_STATUS:", body.get("status", ""))
    print("INVOICE_AMOUNT:", amount.get("currency_code", ""),
          amount.get("value", ""))
    print("INVOICE_DUE:", due.get("currency_code", ""), due.get("value", ""))
    return 0


def main():
    env = load_env()
    if not assert_sandbox(env):
        print("ABORT: PAYPAL_ENV is not 'sandbox' or base is not the sandbox API.")
        return 2
    if len(sys.argv) < 2:
        print("usage: revive_webhook.py patch <base>|invoice <id>")
        return 2
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    try:
        token, _scopes = get_access_token(session, env)
    except RuntimeError as exc:
        print("ABORT:", exc)
        return 1
    if sys.argv[1] == "patch" and len(sys.argv) == 3:
        return cmd_patch(session, token, sys.argv[2])
    if sys.argv[1] == "invoice" and len(sys.argv) == 3:
        return cmd_invoice(session, token, sys.argv[2])
    print("usage: revive_webhook.py patch <base>|invoice <id>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
