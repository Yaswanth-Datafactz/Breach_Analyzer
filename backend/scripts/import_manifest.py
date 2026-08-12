"""Load a manifest.json's ground truth into manifest_identities /
manifest_elements (docs/plan.md §4/§10). Thin CLI -- the real logic is
services/accuracy.py's `import_manifest_into_db` (also called
automatically by execute_accuracy_scoring before every scoring run, so
this script is for operator visibility/idempotence checks, not the only
way the tables get populated).

SCHEMA-DRIVEN UNIQUENESS CAVEAT (read before running this twice against
different manifests): `manifest_identities.person_uid` is UNIQUE by
schema and there is no manifest-origin column on either table (db/
models.py's fixed, assigned schema). Empirically, data/manifest.json and
data/manifest-mini.json share the SAME person_uid namespace P0001-P0040
(both seed=42; the mini profile's generation branches on different
scenario counts partway through the RNG stream, so identical person_uids
do NOT always carry identical identity attributes across the two files --
verified below at import time, not just asserted). That means:

  - manifest_identities: importing manifest B after manifest A OVERWRITES
    A's version of any uid B also defines -- this is an inherent property
    of the fixed schema (global UNIQUE(person_uid), no origin column), not
    a bug in this script. Uids exclusive to A are left untouched.
  - manifest_elements: no unique constraint at all, so re-importing the
    SAME manifest would just accumulate duplicate rows unless cleared
    first. This script (via import_manifest_into_db) clears-and-reloads
    scoped to exactly the person_uids the GIVEN manifest defines, so a
    disjoint manifest's rows (e.g. the full manifest's P0041-P0160 while
    importing the mini manifest) are never touched.

Practical upshot: for clean scoring, run this immediately before scoring
a given manifest (execute_accuracy_scoring already does), or at minimum
re-run it for whichever manifest you're about to score against -- don't
assume "I imported the full manifest earlier" is still true for the
identities the mini manifest also defines.

Usage:
    backend/.venv/bin/python backend/scripts/import_manifest.py data/manifest.json
    backend/.venv/bin/python backend/scripts/import_manifest.py data/manifest-mini.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import SessionLocal  # noqa: E402
from app.services.accuracy import import_manifest_into_db, load_manifest, resolve_manifest_path  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest_path", help="path to manifest.json (repo-root-relative or absolute)")
    args = parser.parse_args()

    resolved = resolve_manifest_path(args.manifest_path)
    if not resolved.is_file():
        sys.exit(f"manifest not found: {resolved}")

    manifest = load_manifest(resolved)
    print(f"manifest: {resolved.relative_to(REPO_ROOT) if resolved.is_relative_to(REPO_ROOT) else resolved}")
    print(
        f"  seed={manifest.get('seed')} profile={manifest.get('profile')} "
        f"identities={len(manifest.get('identities', []))} documents={len(manifest.get('documents', []))}"
    )

    db = SessionLocal()
    try:
        summary = import_manifest_into_db(db, manifest, manifest_path=args.manifest_path)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("\nimport summary:")
    print(f"  identities upserted: {summary.identities_upserted}")
    print(f"  elements deleted (previous rows for these uids): {summary.elements_deleted}")
    print(f"  elements inserted: {summary.elements_inserted}")
    if summary.overlapping_uid_conflicts:
        print(
            f"  WARNING: {len(summary.overlapping_uid_conflicts)} person_uid(s) already existed with "
            f"DIFFERENT attributes (overwritten by this import -- see this file's module docstring):"
        )
        for uid in summary.overlapping_uid_conflicts[:20]:
            print(f"    {uid}")
        if len(summary.overlapping_uid_conflicts) > 20:
            print(f"    ... and {len(summary.overlapping_uid_conflicts) - 20} more")
    else:
        print("  no cross-manifest person_uid conflicts (idempotent re-import or a fresh uid range).")


if __name__ == "__main__":
    main()
