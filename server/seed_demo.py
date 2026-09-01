"""Demo seeder: populate the portal with a realistic-looking set of VulnScans
plus one Project (product page) per distinct product, so a fresh install has
something to look at. Idempotent via the notes marker below — re-running
wipes and re-inserts the demo rows only.

The data below is entirely fictional. Swap in your own pipe-delimited rows
(same column order as the header comment) to seed from a real inventory —
but keep real findings out of version control.

Run inside the server container:
    docker compose cp server/seed_demo.py server:/srv/seed_demo.py
    docker compose exec server python /srv/seed_demo.py
"""
import os

from app.database import SessionLocal
from app import models

# Owner of the seeded rows. Falls back to the first admin if unset/not found.
OWNER_EMAIL = os.environ.get("SEED_OWNER_EMAIL", "admin@example.com")
MARKER = "demo-seed-v1"

# product | scan_target | harness | scan_by | results_file | spreadsheet_link | triaged_by | findings | fp | sbp | tp | duplicates | untriaged | highest_severity
DATA = r"""
Acme Gateway 2.1|Binary|None (prompt)|analyst-one|gateway_binary_scan.md|gateway-2-1-findings.xlsx|PSIRT|12|4|3|5|0|0|Medium
Acme Gateway 2.1|Source Code|Sample Harness|analyst-one|gateway_source_scan.md|gateway-2-1-findings.xlsx|PSIRT|8|2|1|5|0|0|High
Acme Gateway 2.2|Black Box|None (prompt)|analyst-two|gateway_blackbox.md||PSIRT|20|11|0|4|1|4|Medium
Acme Mailer 4.0|Source Code|Sample Harness|analyst-two|mailer_summary.md|mailer-4-0-findings.xlsx|Security Champion|15|6|2|7|0|0|High
Acme Mailer 4.0|Binary|None (prompt)|analyst-three|mailer_binary.md|mailer-4-0-findings.xlsx|Security Champion|9|5|1|3|0|0|Low
Acme Vault 1.5|Source Code|Sample Harness|analyst-three|vault_summary.md|vault-1-5-findings.xlsx|PSIRT|25|3|4|10|2|6|Critical
Acme Vault 1.5|Source Code + Dynamic Test|None (prompt)|analyst-four|vault_dynamic.md|vault-1-5-findings.xlsx|PSIRT|18|9|0|9|0|0|High
Acme Portal 3.0|Source Code|None (long prompt)|analyst-four|portal_long_prompt.md||Dev Team Lead|40|0|0|0|0|40|N/A
Acme Portal 3.0|Source Code|None (short prompt)|analyst-five|portal_short_prompt.md||Dev Team Lead|22|0|0|0|0|22|N/A
Acme Portal 3.0|Source Code|Sample Harness|analyst-five|portal_harness.md|portal-3-0-findings.xlsx|Dev Team Lead|31|7|5|14|1|4|High
Acme Relay 1.0|Binary|Sample Harness|analyst-six|relay_summary.md||Security Champion|6|1|0|2|0|3|Low
Acme Relay 1.0|Black Box|None (prompt)|analyst-six|relay_blackbox.md||Security Champion|11|8|1|2|0|0|Medium
Acme Insight 5.2|Source Code|Sample Harness|analyst-seven|insight_harness.md|insight-5-2-findings.xlsx|Dev Team Lead|17|2|3|9|0|3|High
Acme Insight 5.2|Source Code + Dynamic Test|None (prompt)|analyst-seven|insight_dynamic.md||Dev Team Lead|13|4|0|6|0|3|Medium
Acme Edge 1.1|Source Code|None (prompt)|analyst-eight|edge_review.md||PSIRT|0|0|0|0|0|0|N/A
"""

SEV = {
    "critical": models.Severity.critical, "high": models.Severity.high,
    "medium": models.Severity.medium, "low": models.Severity.low,
    "info": models.Severity.info,
}


def sev(s: str) -> models.Severity:
    return SEV.get(s.strip().lower(), models.Severity.unknown)


def num(s: str) -> int:
    s = s.strip()
    if s in ("", "?", "#CONNECT!", "#VALUE!", "N/A"):
        return 0
    try:
        return int(s)
    except ValueError:
        return 0


def main():
    db = SessionLocal()
    owner = db.query(models.User).filter(models.User.email == OWNER_EMAIL).first()
    if owner is None:
        owner = db.query(models.User).filter(models.User.role == models.Role.admin).first()
    if owner is None:
        raise SystemExit("no admin user to own the demo data")
    print(f"owner: {owner.email}")

    # wipe any prior run of this seeder
    old = db.query(models.VulnScan).filter(models.VulnScan.notes == MARKER).all()
    for s in old:
        db.delete(s)
    db.commit()
    print(f"removed {len(old)} prior demo scan(s)")

    projects: dict[str, models.Project] = {}

    def get_project(name: str) -> models.Project:
        if name in projects:
            return projects[name]
        p = db.query(models.Project).filter(models.Project.name == name).first()
        if p is None:
            p = models.Project(name=name, description="Seeded by seed_demo.py (fictional demo data).",
                               created_by=owner.id)
            db.add(p)
            db.flush()
            if owner not in p.members:
                p.members.append(owner)
        projects[name] = p
        return p

    rows = [ln for ln in DATA.strip().splitlines() if ln.strip()]
    total_f = n = 0
    for ln in rows:
        c = [x.strip() for x in ln.split("|")]
        if len(c) < 14:
            print(f"SKIP malformed ({len(c)} cols): {ln[:60]}")
            continue
        product = c[0]
        proj = get_project(product)
        scan = models.VulnScan(
            user_id=owner.id, project_id=proj.id, product=product,
            state=models.ScanState.confirmed,
            scan_target=c[1], harness_used=c[2], scan_by=c[3],
            results_file=c[4], spreadsheet_link=c[5], triaged_by=c[6],
            findings=num(c[7]), fp=num(c[8]), sbp=num(c[9]), tp=num(c[10]),
            duplicates=num(c[11]), untriaged=num(c[12]),
            highest_severity=sev(c[13]), notes=MARKER,
        )
        db.add(scan)
        total_f += num(c[7])
        n += 1

    db.commit()
    print(f"inserted {n} scans across {len(projects)} products")
    print(f"total findings = {total_f}")


if __name__ == "__main__":
    main()
