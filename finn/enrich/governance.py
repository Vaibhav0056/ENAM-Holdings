"""Governance red flags (spec P3): auditor resignation, senior-management exits, promoter pledge changes,
related-party deals, regulatory or legal action. Flags come from the filing type and its own wording (rules) and from
verified management-change extraction (senior exits); each flag points at the filing it rests on."""
from __future__ import annotations

import re

FLAG_RULES = [
    ("auditor_resignation", "high", r"resignation of (the )?(statutory |secretarial |internal |joint )?auditor|auditors?\b.{0,40}\bresign"),
    ("pledge_change", "medium", r"(creation|invocation) of (pledge|encumbrance)|reg(ulation|\.)?\s*31\s*\(1\)"),
    ("related_party", "medium", r"related party transaction"),
    ("regulatory_action", "high", r"show cause|sebi order|penalt|warning letter|form 483|search and seizure|enforcement directorate|\bcbi\b"),
    ("insolvency", "high", r"insolvency|\bcirp\b|nclt|nclat"),
    ("default", "high", r"default (in|on) (payment|repayment)"),
    ("fraud", "high", r"\bfraud\b|forensic"),
    ("whistleblower", "medium", r"whistle-?blower"),
    ("modified_opinion", "high", r"qualified (audit )?opinion|modified opinion|emphasis of matter"),
]
GOVERNANCE_TYPES = {"governance", "litigation_regulatory", "insider_pledge", "mgmt_change"}


def rule_flags(ftype: str, subject: str | None, body: str | None) -> list[dict]:
    if ftype not in GOVERNANCE_TYPES:
        return []
    text = f"{subject or ''} {body or ''}"
    out = []
    for kind, severity, pat in FLAG_RULES:
        if re.search(pat, text, re.I):
            out.append({"kind": kind, "severity": severity, "summary": re.sub(r"\s+", " ", (body or subject or ""))[:200],
                        "source": "filing wording"})
    return out
