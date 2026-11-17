#!/usr/bin/env bash
# Restore a saved Governance Hub database state.
# Run: bash governance-hub/restore-current-state.sh check|restore [demo|original]
# The default "demo" snapshot has 13 entries; "original" has one.
# "restore" first saves a fresh backup of the live database beside the snapshot.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
namespace="doc-gen-demo"
target="deploy/governance-hub"
container="governance-hub"

if [[ "${1:-}" != check && "${1:-}" != restore ]] || [[ $# -gt 2 ]]; then
  echo "Usage: $0 check|restore [demo|original]" >&2
  exit 2
fi

case "${2:-demo}" in
  demo)
    snapshot="$script_dir/snapshots/governance-2026-11-16-13-entries.db"
    expected_sha256="e4829790fd67b35e1401dd44263e1b8396c1d25625f1a9fe1e9479030161e967"
    expected_repos=13
    expected_runs=13
    expected_demo=12
    ;;
  original)
    snapshot="$script_dir/snapshots/governance-2026-11-16.db"
    expected_sha256="251c2d81b5d42dc4433498014321da272a6fb7c9850e7443bd46ece25ca531c8"
    expected_repos=1
    expected_runs=1
    expected_demo=0
    ;;
  *)
    echo "Unknown snapshot: $2" >&2
    exit 2
    ;;
esac

# Check both file integrity and the expected point-in-time contents.
python3 - "$snapshot" "$expected_sha256" "$expected_repos" "$expected_runs" "$expected_demo" <<'PY'
import hashlib
import sqlite3
import sys

path, expected_hash = sys.argv[1:3]
expected_repos, expected_runs, expected_demo = map(int, sys.argv[3:])
with open(path, "rb") as file:
    actual_hash = hashlib.file_digest(file, "sha256").hexdigest()
if actual_hash != expected_hash:
    raise SystemExit(f"Snapshot SHA-256 mismatch: {actual_hash}")
with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    repos = [row[0] for row in db.execute("SELECT repo_url FROM tracked_repos")]
    runs = db.execute("SELECT count(*) FROM agent_runs").fetchone()[0]
    passing_demo = db.execute(
        """SELECT count(*) FROM tracked_repos WHERE repo LIKE ?
           AND has_architecture_doc=1 AND has_openapi_spec=1
           AND compliance_scan_has_violations=0""", ("%-demo",)
    ).fetchone()[0]
    assert "https://github.com/matteo-grimaldi/spring-petclinic-rest-legacy" in repos
    assert (len(repos), runs, passing_demo) == (expected_repos, expected_runs, expected_demo)
print(f"Snapshot verified: {len(repos)} repositories, {runs} runs, {passing_demo} passing demo entries")
PY

if [[ "$1" == check ]]; then
  exit 0
fi

command -v oc >/dev/null
oc -n "$namespace" get "$target" >/dev/null
backup="$script_dir/snapshots/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).db"
if [[ -e "$backup" ]]; then
  echo "Backup already exists: $backup" >&2
  exit 1
fi

# SQLite's online backup API captures a consistent database, including any
# changes that have not yet been checkpointed from its WAL.
oc -n "$namespace" exec "$target" -c "$container" -- python3 -c '
import os, sqlite3, sys, tempfile
live = sqlite3.connect("file:" + os.environ["DATABASE_PATH"] + "?mode=ro", uri=True)
fd, path = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    with sqlite3.connect(path) as copy:
        live.backup(copy)
    with open(path, "rb") as file:
        sys.stdout.buffer.write(file.read())
finally:
    live.close()
    os.unlink(path)
' > "$backup"

python3 - "$backup" <<'PY'
import sqlite3
import sys
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as db:
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Pre-restore backup failed integrity check; restore cancelled")
print(f"Pre-restore backup saved: {sys.argv[1]}")
PY

# Stream the snapshot into the pod and use SQLite's backup API to replace the
# live database transactionally. Restart to drop the app's open connections.
oc -n "$namespace" exec -i "$target" -c "$container" -- python3 -c '
import os, sqlite3, sys, tempfile
fd, path = tempfile.mkstemp(suffix=".db")
try:
    with os.fdopen(fd, "wb") as file:
        file.write(sys.stdin.buffer.read())
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("Incoming snapshot failed integrity check")
        with sqlite3.connect(os.environ["DATABASE_PATH"], timeout=30) as live:
            source.backup(live)
            if live.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("Restored database failed integrity check")
finally:
    os.unlink(path)
' < "$snapshot"

oc -n "$namespace" rollout restart "$target"
oc -n "$namespace" rollout status "$target" --timeout=180s
oc -n "$namespace" exec "$target" -c "$container" -- python3 -c '
import os, sqlite3, sys
expected_repos, expected_runs, expected_demo = map(int, sys.argv[1:])
with sqlite3.connect("file:" + os.environ["DATABASE_PATH"] + "?mode=ro", uri=True) as db:
    repos = [row[0] for row in db.execute("SELECT repo_url FROM tracked_repos")]
    runs = db.execute("SELECT count(*) FROM agent_runs").fetchone()[0]
    passing_demo = db.execute(
        """SELECT count(*) FROM tracked_repos WHERE repo LIKE ?
           AND has_architecture_doc=1 AND has_openapi_spec=1
           AND compliance_scan_has_violations=0""", ("%-demo",)
    ).fetchone()[0]
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "https://github.com/matteo-grimaldi/spring-petclinic-rest-legacy" in repos
    assert (len(repos), runs, passing_demo) == (expected_repos, expected_runs, expected_demo)
print(f"Restore verified: {len(repos)} repositories, {runs} runs, {passing_demo} passing demo entries")
' "$expected_repos" "$expected_runs" "$expected_demo"
echo "Restore complete. Pre-restore backup: $backup"
