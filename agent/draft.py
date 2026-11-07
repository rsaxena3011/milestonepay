"""Milestone draft data model for the MilestonePay agent.

Honesty discipline (the project's standing rule, from
MERGED_PLAN_2026-10-30.md): an extractor NEVER invents a value.
A field the evidence does not support stays None, carries
confidence 0.0 and an explanatory note, and produces a flag on
the draft. Blocking flags (client, milestone title, deliverables,
amount, currency missing) stop the hand-off from creating any
invoice, even after approval — deterministic code validates before
the API call, exactly as the plan locks it.
"""

import re
import time
import uuid
from dataclasses import dataclass, field as dc_field

# Field names every provider must fill (value may legitimately be
# None when the evidence does not support it).
FIELD_NAMES = (
    "client",
    "client_email",
    "milestone_title",
    "deliverables",
    "amount",
    "currency",
    "due_terms",
)

# Wording that makes a claimed deliverable ambiguous: it asserts
# work a client could dispute. Surfaced as a flag for the human
# approver; the deliverable text itself is carried verbatim.
AMBIGUITY_PATTERNS = (
    r"\betc\b",
    r"\bmisc\b",
    r"\bother stuff\b",
    r"\bas discussed\b",
    r"\btbd\b",
    r"\bvarious\b",
    r"\band so on\b",
    r"\bstuff\b",
)


def find_ambiguity(line):
    """Return the matched vague marker in a deliverable line, or None."""
    low = " " + str(line).lower() + " "
    for pat in AMBIGUITY_PATTERNS:
        m = re.search(pat, low)
        if m:
            return m.group(0).strip()
    return None


@dataclass
class FieldExtraction:
    """One extracted field: value, confidence, and its receipt.

    value        the extracted value, or None when unsupported
    confidence   0.0-1.0, the provider's stated confidence
    source_quote the exact evidence substring the value came from,
                 or None when there is none (value must then be None
                 for scalar fields: no quote, no claim)
    note         how the value was derived, or why it is missing
    """

    value: object = None
    confidence: float = 0.0
    source_quote: str = None
    note: str = ""

    @property
    def present(self):
        return self.value is not None and self.value != "" and self.value != []


@dataclass
class MilestoneDraft:
    """A milestone extracted from evidence. A draft is ONLY a draft:

    status moves DRAFT -> APPROVED (via approval.approve_draft,
    which also writes the audit line) or DRAFT -> REJECTED. The
    hand-off refuses anything whose status is not APPROVED.
    """

    evidence_text: str
    provider_name: str
    fields: dict
    evidence_source: str = "inline"
    draft_id: str = dc_field(
        default_factory=lambda: "DRAFT-" + uuid.uuid4().hex[:8])
    flags: list = dc_field(default_factory=list)
    status: str = "DRAFT"
    approval: dict = None
    created_ts: float = dc_field(default_factory=time.time)

    def get(self, name):
        return self.fields[name]

    def blocking_flags(self):
        return [f for f in self.flags if f.startswith("BLOCKING:")]

    def summary(self):
        """Plain dict of the headline values (for logs and the demo)."""
        out = {"draft_id": self.draft_id, "status": self.status,
               "provider": self.provider_name}
        for name in FIELD_NAMES:
            out[name] = self.fields[name].value
        return out


def compute_flags(draft):
    """Derive the draft's flag list from its fields. Shared by every
    provider so the honesty rules cannot drift between providers."""
    flags = []
    f = draft.fields
    if not f["client"].present:
        flags.append(
            "BLOCKING: client not identified in the evidence; "
            "an invoice needs a named recipient.")
    if not f["milestone_title"].present:
        flags.append(
            "BLOCKING: milestone title not identified in the evidence.")
    deliverables = f["deliverables"].value or []
    if not deliverables:
        flags.append(
            "BLOCKING: no deliverables claimed in the evidence; "
            "MilestonePay invoices on evidence, not on trust.")
    if f["amount"].value is None:
        flags.append(
            "BLOCKING: amount not stated in the evidence; it is "
            "never guessed. A human must supply it before invoicing.")
    elif not f["currency"].present:
        flags.append(
            "BLOCKING: amount found but its currency is not stated "
            "in the evidence.")
    for line in deliverables:
        marker = find_ambiguity(line)
        if marker:
            flags.append(
                "Ambiguous deliverable wording (%s): %r — confirm "
                "scope with the client before invoicing."
                % (marker, line))
    if not f["client_email"].present:
        flags.append(
            "Client email not in the evidence; an invoice draft can "
            "be created but cannot be sent until an email is supplied.")
    if not f["due_terms"].present:
        flags.append(
            "Due terms not stated in the evidence; the invoice will "
            "carry DUE_ON_RECEIPT unless a human sets terms first.")
    return flags
