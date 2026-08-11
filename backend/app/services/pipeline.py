"""Bulk pipeline orchestration (docs/plan.md D2: UC2's proven async
job-runner pattern -- status rows + startup stale-job reaper; Celery/arq
named as the explicit 100x-load upgrade path in §12, not built now).

Phase A scaffold: only the startup reaper stub lives here. The real
ingest -> parse -> tier 0 -> tier 1/2 -> ER orchestration lands in phase
B1/B2 (docs/plan.md §14, Wed) -- no placeholder pipeline logic that could
be mistaken for working extraction.
"""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("pipeline")


def reap_stale_jobs() -> int:
    """Mark any non-terminal extraction_jobs row 'failed' on process startup.

    A non-terminal row can only be left over from a process that died
    mid-job (killed, crashed, redeployed) -- never a job this fresh process
    is already tracking, since it just started. UC2's reaper, keyed off
    EXTRACTION_JOB_TERMINAL_STATUSES and the partial unique index in
    db/models.py.

    Phase A stub: the real UPDATE lands with the pipeline in phase B1 --
    today there is nothing that creates jobs, so there is nothing to reap.
    Logs and returns 0 so main.py's lifespan wiring is real from day one.
    """
    logger.info("reaped_stale_jobs", count=0, stub=True)
    return 0
