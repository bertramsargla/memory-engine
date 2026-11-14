"""Pattern-based redaction of credentials and payment/identity numbers before text is stored."""
from __future__ import annotations

import re


def redact(text: str) -> str:
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED SSN]", text)
    text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[REDACTED NUMBER]", text)
    text = re.sub(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b", "[REDACTED IBAN]", text)
    text = re.sub(r"(?i)\b(api[_ -]?key|password|secret|token)\s*[:=]\s*[^\s]+", r"\1=[REDACTED]", text)
    # Raw credentials printed without a "key=" label (terminals show these all the time)
    text = re.sub(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                  r"|GOCSPX-[A-Za-z0-9_\-]{10,}|AIza[0-9A-Za-z_\-]{30,}|xox[abpr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})",
                  "[REDACTED TOKEN]", text)
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED KEY]", text, flags=re.S)
    text = re.sub(r"(?i)\b(cvv|cvc|security code)\s*[:=]?\s*\d{3,4}\b", r"\1=[REDACTED]", text)
    return text
