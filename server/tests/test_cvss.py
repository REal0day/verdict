"""CVSS scoring — 3.1 computed against reference vectors, 4.0 shape validation."""
import os, sys, pathlib, base64, secrets
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "t")
import pytest
from app import cvss


@pytest.mark.parametrize("vector,score,sev", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8, "critical"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", 7.5, "high"),
    ("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N", 3.7, "low"),
    ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:L/I:L/A:N", 6.4, "medium"),
    ("CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N", 0.0, "none"),
])
def test_cvss31_matches_reference(vector, score, sev):
    s, se, _ = cvss.cvss31_base(vector)
    assert s == score and se == sev


def test_cvss31_rejects_bad_prefix():
    with pytest.raises(cvss.CVSSError):
        cvss.cvss31_base("CVSS:2.0/AV:N")


def test_cvss31_rejects_missing_metric():
    with pytest.raises(cvss.CVSSError, match="missing metric"):
        cvss.cvss31_base("CVSS:3.1/AV:N/AC:L")


def test_cvss31_rejects_bad_value():
    with pytest.raises(cvss.CVSSError, match="invalid value"):
        cvss.cvss31_base("CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")


def test_cvss40_validates_shape():
    m = cvss.validate_cvss40("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N")
    assert m["AV"] == "N" and m["VC"] == "H"


def test_cvss40_rejects_missing_metric():
    with pytest.raises(cvss.CVSSError):
        cvss.validate_cvss40("CVSS:4.0/AV:N/AC:L")


def test_severity_bands():
    assert cvss.severity_from_score(0) == "none"
    assert cvss.severity_from_score(3.9) == "low"
    assert cvss.severity_from_score(4.0) == "medium"
    assert cvss.severity_from_score(9.0) == "critical"
