"""Human approval gate for MilestonePay drafts.

A draft is only a draft. The ONLY way a draft becomes eligible for
invoice creation is approve_draft(), called explicitly by a human
(or by a CLI flag in a human-run command). Approval is recorded
twice: on the draft object (status + approval record) and as an
append-only JSONL audit line, mirroring webhook_listener's audit
discipline (one JSON object per line, `ts` first-class, keys
sorted, file append-only). The hand-off checks the draft's status
itself, so an approval that exists only in a log line is not
enough, and an approved status with no audit line should never
occur through this module.
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)
DEFAULT_AUDIT_PATH = os.path.join(FOLDER, "agent_audit.jsonl")


def _audit(record, audit_path):
    record = {"ts": round(time.time(), 3), **record}
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def _base_record(action, draft, actor):
    fields = draft.fields
    return {
        "action": action,
        "actor": actor,
        "draft_id": draft.draft_id,
        "provider": draft.provider_name,
        "evidence_source": draft.evidence_source,
        "client": fields["client"].value,
        "amount": fields["amount"].value,
        "currency": fields["currency"].value,
        "flags": list(draft.flags),
    }


def approve_draft(draft, approver, audit_path=DEFAULT_AUDIT_PATH,
                  note=""):
    """Record a human approval. Raises if the draft is not DRAFT."""
    if draft.status != "DRAFT":
        raise ValueError(
            "draft %s is %s; only a DRAFT can be approved"
            % (draft.draft_id, draft.status))
    if not approver:
        raise ValueError("an approver identity is required")
    draft.status = "APPROVED"
    draft.approval = {"by": approver, "ts": round(time.time(), 3),
                      "note": note}
    record = _base_record("draft_approved", draft, approver)
    if note:
        record["note"] = note
    return _audit(record, audit_path)


def reject_draft(draft, rejector, audit_path=DEFAULT_AUDIT_PATH,
                 note=""):
    """Record a human rejection. A rejected draft can never invoice."""
    if draft.status != "DRAFT":
        raise ValueError(
            "draft %s is %s; only a DRAFT can be rejected"
            % (draft.draft_id, draft.status))
    if not rejector:
        raise ValueError("a rejector identity is required")
    draft.status = "REJECTED"
    draft.approval = {"by": rejector, "ts": round(time.time(), 3),
                      "note": note, "rejected": True}
    record = _base_record("draft_rejected", draft, rejector)
    if note:
        record["note"] = note
    return _audit(record, audit_path)
