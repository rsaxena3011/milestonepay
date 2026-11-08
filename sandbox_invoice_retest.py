#!/usr/bin/env python3
"""C016 MilestonePay: sandbox invoicing scope retest, 2026-11-02.

Purpose: prove end to end that enabling the Invoicing capability on the
"MilestonePay Sandbox" app fixed the 2026-11-01 HTTP 403 on invoice draft
creation (see INVOICE_403_NOTE_2026-11-01.md).

Flow, exactly one attempt per step, no retries, no redirects:
  1. Fresh client-credentials OAuth token (sandbox).
  2. Report the granted scope list and whether the invoicing scope is in it.
     The token value itself is never printed or written anywhere.
  3. If the scope is present, create one minimal invoice DRAFT with a
     sandbox placeholder recipient (no real person).
  4. Send the draft inside the sandbox, then read the invoice back and
     require its canonical status to be SENT before claiming success.

Credentials are read from .env next to this script and stay in memory.
Only HTTP status codes, scope names, error names and details, and the
invoice id are printed. Transport errors print only the step label and the
exception class name. Run with the workspace venv python.

Revised after review round 1 (2026-11-02): read-back failure branch and
SENT status validation, transport error wrapping, guarded JSON parsing,
payment_term field fix, empty token and id checks, send error debug_id,
per-run unique invoice number and request ids, BASE assertion, redirects
off, retries disabled, .env quote tolerance, scope and details typing
guards, configuration block removed, create success code tightened to
201. Send success accepts 200, 202, or 204 (200 observed live on
2026-11-02) because the read back SENT check is the binding proof.
A post-run fix the same day taught the script the create response's
HATEOAS link shape, after the live run proved the chain but exited at
the id extraction step (see INVOICE_SCOPE_RETEST_2026-11-02.md).
"""

import os
import re
import sys
import urllib.parse
import uuid

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
BASE = "https://api-m.sandbox.paypal.com"
INVOICING_SCOPE = "https://uri.paypal.com/services/invoicing"


def load_env(path):
    env = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            env[key] = value
    return env


def parse_json(resp):
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def extract_invoice_id_from_href(href):
    if not isinstance(href, str) or not href:
        return ""
    try:
        parts = urllib.parse.urlsplit(href)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.query or parts.fragment:
        return ""
    if parts.netloc not in ("api-m.sandbox.paypal.com", "api.sandbox.paypal.com"):
        return ""
    segments = [seg for seg in parts.path.split("/") if seg]
    if segments[:3] != ["v2", "invoicing", "invoices"] or len(segments) != 4:
        return ""
    candidate = segments[3]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", candidate):
        return ""
    return candidate


def print_error_details(err, prefix):
    details = err.get("details", [])
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict):
                print(prefix + "_ERROR_DETAIL:", d.get("issue", ""), "|", d.get("description", ""))


def main():
    if BASE != "https://api-m.sandbox.paypal.com":
        print("ABORT: BASE is not the sandbox API.")
        return 2
    try:
        env = load_env(ENV_PATH)
    except OSError:
        print("ABORT: could not read .env.")
        return 2
    paypal_env = env.get("PAYPAL_ENV", "")
    if paypal_env.lower() != "sandbox":
        print("ABORT: PAYPAL_ENV is not 'sandbox'; refusing to run.")
        return 2
    client_id = env.get("PAYPAL_CLIENT_ID", "")
    client_secret = env.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ABORT: client id or secret missing from .env.")
        return 2

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    # Step 1: fresh OAuth token (one fetch only).
    try:
        tok = session.post(
            BASE + "/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=30,
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as exc:
        print("TOKEN_TRANSPORT_ERROR:", type(exc).__name__)
        return 1
    print("TOKEN_HTTP_STATUS:", tok.status_code)
    tok_body = parse_json(tok)
    if tok.status_code != 200:
        err = tok_body or {}
        print("TOKEN_ERROR_NAME:", err.get("error", "unknown"))
        print("TOKEN_ERROR_DESCRIPTION:", err.get("error_description", ""))
        return 1
    if tok_body is None:
        print("TOKEN_PARSE_ERROR: token response was not a JSON object.")
        return 1
    access_token = tok_body.get("access_token", "")
    if not access_token:
        print("RESULT: token response had no access token.")
        return 1
    scopes = tok_body.get("scope", "")
    if not isinstance(scopes, str):
        scopes = " ".join(scopes) if isinstance(scopes, list) else ""
    print("TOKEN_TYPE:", tok_body.get("token_type", ""))
    print("TOKEN_EXPIRES_IN:", tok_body.get("expires_in", ""))
    print("GRANTED_SCOPES:", scopes)
    invoicing_present = INVOICING_SCOPE in scopes.split()
    print("INVOICING_SCOPE_PRESENT:", invoicing_present)
    if not invoicing_present:
        print("RESULT: scope still absent; draft test not attempted.")
        return 3

    run_suffix = uuid.uuid4().hex[:8]
    auth_headers = {
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
    }
    create_headers = dict(auth_headers)
    create_headers["PayPal-Request-Id"] = "C016-RETEST-2026-11-02-" + run_suffix

    # Step 2: one invoice draft attempt, minimal payload, sandbox recipient.
    payload = {
        "detail": {
            "invoice_number": "C016-RETEST-" + run_suffix,
            "currency_code": "USD",
            "note": "MilestonePay sandbox invoicing scope retest, 2026-11-02.",
            "payment_term": {"term_type": "NET_10"},
        },
        "invoicer_info": {
            "name": {"given_name": "MilestonePay", "surname": "Sandbox"},
        },
        "primary_recipients": [
            {
                "billing_info": {
                    "name": {"given_name": "Sandbox", "surname": "Buyer"},
                    "email_address": "c016-buyer@personal.example.com",
                }
            }
        ],
        "items": [
            {
                "name": "Milestone 1: evidence pack delivery",
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": "100.00"},
            }
        ],
    }
    try:
        draft = session.post(
            BASE + "/v2/invoicing/invoices",
            headers=create_headers,
            json=payload,
            timeout=30,
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as exc:
        print("DRAFT_TRANSPORT_ERROR:", type(exc).__name__)
        return 4
    print("DRAFT_HTTP_STATUS:", draft.status_code)
    draft_body = parse_json(draft)
    if draft.status_code != 201:
        err = draft_body or {}
        print("DRAFT_ERROR_NAME:", err.get("name", "unknown"))
        print("DRAFT_ERROR_MESSAGE:", err.get("message", ""))
        print("DRAFT_ERROR_DEBUG_ID:", err.get("debug_id", ""))
        print_error_details(err, "DRAFT")
        return 4
    if draft_body is None:
        print("DRAFT_PARSE_ERROR: draft response was not a JSON object.")
        return 4
    invoice_id = draft_body.get("id", "")
    if not invoice_id:
        # Observed live on 2026-11-02: the sandbox create response is a
        # HATEOAS link object ({"href": ".../invoices/INV2-...", "rel":
        # "self", "method": "GET"}) with no top level id field. The id is
        # the path segment after "invoices" in that href. The created
        # draft was confirmed independently in the invoice list (see
        # INVOICE_SCOPE_RETEST_2026-11-02.md). The href is validated
        # strictly (patch review round 1): parsed URL, sandbox host, no
        # query or fragment, exact path shape, conservative id pattern.
        # Any mismatch leaves invoice_id empty and the exit 4 path fires.
        invoice_id = extract_invoice_id_from_href(draft_body.get("href", ""))
    if not invoice_id:
        print("RESULT: draft response had no invoice id.")
        return 4
    print("DRAFT_INVOICE_ID:", invoice_id)
    print("DRAFT_STATUS:", draft_body.get("status", ""))

    # Step 3: send the draft inside the sandbox (one attempt).
    send_headers = dict(auth_headers)
    send_headers["PayPal-Request-Id"] = "C016-SEND-2026-11-02-" + run_suffix
    try:
        send = session.post(
            BASE + "/v2/invoicing/invoices/" + invoice_id + "/send",
            headers=send_headers,
            json={"send_to_invoicer": True, "send_to_recipient": True},
            timeout=30,
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as exc:
        print("SEND_TRANSPORT_ERROR:", type(exc).__name__)
        return 5
    print("SEND_HTTP_STATUS:", send.status_code)
    # Send success observed live in this sandbox on 2026-11-02: HTTP 200
    # (a link body), not the 204 some references describe. 200, 202, and
    # 204 are accepted; the read back SENT check below is the real proof,
    # so a loose send code cannot create a false success.
    if send.status_code not in (200, 202, 204):
        err = parse_json(send) or {}
        print("SEND_ERROR_NAME:", err.get("name", "unknown"))
        print("SEND_ERROR_MESSAGE:", err.get("message", ""))
        print("SEND_ERROR_DEBUG_ID:", err.get("debug_id", ""))
        print_error_details(err, "SEND")
        return 5

    # Step 4: read the invoice back; the canonical state is the proof.
    try:
        read = session.get(
            BASE + "/v2/invoicing/invoices/" + invoice_id,
            headers=auth_headers,
            timeout=30,
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as exc:
        print("READ_TRANSPORT_ERROR:", type(exc).__name__)
        return 6
    print("READ_HTTP_STATUS:", read.status_code)
    read_body = parse_json(read)
    if read.status_code != 200:
        err = read_body or {}
        print("READ_ERROR_NAME:", err.get("name", "unknown"))
        print("READ_ERROR_MESSAGE:", err.get("message", ""))
        print("READ_ERROR_DEBUG_ID:", err.get("debug_id", ""))
        print_error_details(err, "READ")
        print("RESULT: read back failed; send not proven.")
        return 6
    if read_body is None:
        print("READ_PARSE_ERROR: read response was not a JSON object.")
        return 6
    read_status = read_body.get("status", "")
    print("READ_INVOICE_STATUS:", read_status)
    amount = read_body.get("amount", {})
    if isinstance(amount, dict):
        print("READ_AMOUNT:", amount.get("currency_code", ""), amount.get("value", ""))
    due = read_body.get("due_amount", {})
    if isinstance(due, dict):
        print("READ_DUE_AMOUNT:", due.get("currency_code", ""), due.get("value", ""))
    if read_status != "SENT":
        print("RESULT: read back status was not SENT; send not proven.")
        return 6
    print("RESULT: draft created, sent, and read back as SENT inside the sandbox.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
