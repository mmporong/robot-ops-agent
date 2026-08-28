from __future__ import annotations

import re
from pathlib import Path


SENSITIVE_BASENAMES = frozenset(
    {
        ".env",
        ".netrc",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
SENSITIVE_PATH_TOKENS = ("credential", "secret")

# The indexer and every read-only tool share this exact detector.  A document
# containing one of these values is excluded wholesale; tool output is redacted.
SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?):\/\/[^\s]+"),
    re.compile(
        r"(?im)^\s*(?:export\s+)?(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)\s*[:=]\s*[\"']?[^\s\"'{}<>]{8,}[^\r\n]*$"
    ),
    re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_.-]*(?:TOKEN|PASSWORD|API[_-]?KEY|SECRET)\s*[:=]\s*[\"']?[^\s\"'{}<>]{8,}[^\r\n]*$"
    ),
)


def path_is_sensitive(path: Path, root: Path) -> bool:
    relative_parts = tuple(part.lower() for part in path.relative_to(root).parts)
    lower_name = path.name.lower()
    if lower_name in SENSITIVE_BASENAMES:
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    if any(
        part in {".aws", ".gnupg", ".kube", ".ssh", "credentials", "secrets"}
        for part in relative_parts
    ):
        return True
    return any(
        token in part for part in relative_parts for token in SENSITIVE_PATH_TOKENS
    )


def contains_sensitive_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in SENSITIVE_TEXT_PATTERNS)


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def key_is_sensitive(key: str) -> bool:
    normalized = key.upper().replace("-", "_")
    return normalized.endswith(("_TOKEN", "_PASSWORD", "_API_KEY", "_SECRET")) or normalized in {
        "TOKEN",
        "PASSWORD",
        "API_KEY",
        "SECRET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }
