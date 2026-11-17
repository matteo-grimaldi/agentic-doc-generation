"""Seed 12 synthetic, passing Governance Hub entries for the demo dashboard.

Run in the Hub container: oc -n doc-gen-demo exec -i deploy/governance-hub \
    -c governance-hub -- python3 - < governance-hub/seed-demo-entries.py

These records are display fixtures. They do not represent real GitHub checks.
The script refuses to overwrite an existing repository or run.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone


APPS = (
    ("matteo-grimaldi", "enterprise-payments-api-demo", "Enterprise Payments API"),
    ("matteo-grimaldi", "customer-profile-service-demo", "Customer Profile Service"),
    ("matteo-grimaldi", "order-fulfillment-service-demo", "Order Fulfillment Service"),
    ("matteo-grimaldi", "inventory-sync-worker-demo", "Inventory Sync Worker"),
    ("matteo-grimaldi", "identity-access-gateway-demo", "Identity Access Gateway"),
    ("matteo-grimaldi", "billing-ledger-api-demo", "Billing Ledger API"),
    ("mcombi", "enterprise-admin-portal-demo", "Enterprise Admin Portal"),
    ("mcombi", "partner-onboarding-frontend-demo", "Partner Onboarding Frontend"),
    ("mcombi", "customer-self-service-web-demo", "Customer Self Service Web"),
    ("mcombi", "operations-command-center-demo", "Operations Command Center"),
    ("mcombi", "analytics-dashboard-frontend-demo", "Analytics Dashboard Frontend"),
    ("mcombi", "procurement-workflow-ui-demo", "Procurement Workflow UI"),
)

REPORT = """# Synthetic demo compliance report

This is a display fixture. No live repository scan was performed.

| Category | Check | Status | Severity | Details |
| --- | --- | --- | --- | --- |
| K8s/OpenShift manifests | Demo check | PASS | None | Synthetic pass for dashboard preview |
| Project structure | Demo check | PASS | None | Synthetic pass for dashboard preview |
| pom.xml | Demo check | PASS | None | Synthetic pass for dashboard preview |
"""


def main() -> None:
    path = os.environ["DATABASE_PATH"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sqlite3.connect(path, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        existing = {
            row[0] for row in db.execute("SELECT repo_url FROM tracked_repos")
        }
        urls = [f"https://github.com/{owner}/{repo}" for owner, repo, _ in APPS]
        conflicts = existing.intersection(urls)
        if conflicts:
            raise SystemExit(f"Refusing to overwrite existing entries: {sorted(conflicts)}")

        for index, (owner, repo, title) in enumerate(APPS):
            checked = now - timedelta(minutes=index * 3)
            scanned = checked - timedelta(seconds=30)
            first_seen = checked - timedelta(days=10 + index)
            cursor = db.execute(
                """INSERT INTO tracked_repos
                   (owner, repo, repo_url, component_name, component_title,
                    first_seen, has_architecture_doc, has_openapi_spec,
                    last_compliance_check, last_compliance_scan_run,
                    compliance_scan_has_violations)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, 0)""",
                (
                    owner, repo, f"https://github.com/{owner}/{repo}", repo,
                    f"{title} (Demo)", first_seen.isoformat(sep=" "),
                    checked.isoformat(sep=" "), scanned.isoformat(sep=" "),
                ),
            )
            db.execute(
                """INSERT INTO agent_runs
                   (job_id, repo_id, agent_type, status, started_at, completed_at,
                    triggered_by, report_text)
                   VALUES (?, ?, 'compliance-scan', 'completed', ?, ?, 'demo-seed', ?)""",
                (
                    f"demo-seed-{owner}-{repo}", cursor.lastrowid,
                    (scanned - timedelta(seconds=10)).isoformat(sep=" "),
                    scanned.isoformat(sep=" "), REPORT,
                ),
            )
        db.commit()
        count = db.execute("SELECT count(*) FROM tracked_repos").fetchone()[0]
        print(f"Inserted {len(APPS)} synthetic passing entries; {count} tracked repositories total")


if __name__ == "__main__":
    main()
