"""CVSS parsing + scoring.

CVSS 3.1 base score is computed here from the vector — deterministic and
authoritative, so we never trust a model's arithmetic. CVSS 4.0's official base
score needs a large MacroVector lookup table; rather than transcribe it
half-correctly into a security tool, we validate the 4.0 vector's shape and
keep the model's numeric estimate, clearly labelled. A faithful 4.0 calculator
is a follow-up.
"""
from __future__ import annotations

import math

# ---- CVSS 3.1 ----

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}
_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_C = {"N": 0.85, "L": 0.68, "H": 0.5}

# Allowed base-metric values (mandatory metrics for a base vector).
CVSS31_METRICS = {
    "AV": set("NALP"), "AC": set("LH"), "PR": set("NLH"), "UI": set("NR"),
    "S": set("UC"), "C": set("HLN"), "I": set("HLN"), "A": set("HLN"),
}


class CVSSError(ValueError):
    pass


def parse_vector(vector: str, prefix: str) -> dict[str, str]:
    """Parse 'CVSS:3.1/AV:N/...' into {metric: value}. Raises CVSSError."""
    v = (vector or "").strip()
    if not v.upper().startswith(prefix.upper()):
        raise CVSSError(f"vector must start with {prefix}")
    parts = v.split("/")[1:]
    out: dict[str, str] = {}
    for p in parts:
        if ":" not in p:
            raise CVSSError(f"bad metric segment {p!r}")
        k, val = p.split(":", 1)
        out[k.upper()] = val.upper()
    return out


def _roundup(x: float) -> float:
    """CVSS 3.1 spec roundup: up to the nearest 0.1 via integer math."""
    i = int(round(x * 100000))
    if i % 10000 == 0:
        return i / 100000.0
    return (math.floor(i / 10000) + 1) / 10.0


def severity_from_score(score: float) -> str:
    if score <= 0:
        return "none"
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


def cvss31_base(vector: str) -> tuple[float, str, dict[str, str]]:
    """Return (base_score, severity, parsed_metrics) for a CVSS 3.1 vector."""
    m = parse_vector(vector, "CVSS:3.1")
    for metric, allowed in CVSS31_METRICS.items():
        if metric not in m:
            raise CVSSError(f"missing metric {metric}")
        if m[metric] not in allowed:
            raise CVSSError(f"invalid value {m[metric]!r} for {metric}")

    scope_changed = m["S"] == "C"
    pr = (_PR_C if scope_changed else _PR_U)[m["PR"]]
    exploitability = 8.22 * _AV[m["AV"]] * _AC[m["AC"]] * pr * _UI[m["UI"]]

    isc_base = 1 - (1 - _CIA[m["C"]]) * (1 - _CIA[m["I"]]) * (1 - _CIA[m["A"]])
    if scope_changed:
        impact = 7.52 * (isc_base - 0.029) - 3.25 * (isc_base - 0.02) ** 15
    else:
        impact = 6.42 * isc_base

    if impact <= 0:
        score = 0.0
    elif scope_changed:
        score = _roundup(min(1.08 * (impact + exploitability), 10))
    else:
        score = _roundup(min(impact + exploitability, 10))
    return score, severity_from_score(score), m


# ---- CVSS 4.0 (validate shape only) ----

CVSS40_METRICS = {
    "AV": set("NALP"), "AC": set("LH"), "AT": set("NP"), "PR": set("NLH"),
    "UI": set("NPA"), "VC": set("HLN"), "VI": set("HLN"), "VA": set("HLN"),
    "SC": set("HLN"), "SI": set("HLN"), "SA": set("HLN"),
}
# Base metrics that must be present in a 4.0 base vector.
_CVSS40_REQUIRED = set(CVSS40_METRICS)


def validate_cvss40(vector: str) -> dict[str, str]:
    """Validate a CVSS 4.0 base vector's shape. Raises CVSSError. Does not score."""
    m = parse_vector(vector, "CVSS:4.0")
    for metric in _CVSS40_REQUIRED:
        if metric not in m:
            raise CVSSError(f"missing metric {metric}")
    for k, val in m.items():
        allowed = CVSS40_METRICS.get(k)
        if allowed is not None and val not in allowed:
            raise CVSSError(f"invalid value {val!r} for {k}")
    return m
