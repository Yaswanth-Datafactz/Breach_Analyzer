"""Stale gray-band items (docs/plan.md §14c SUSPICIOUS 5a/5b): when a
decision resolves one er_pair, OTHER open er_pair review items that the
decision made moot must close automatically -- so the adjudicator never
re-spends live budget re-answering a pair the pipeline or a human already
settled. And an agent-driven merge (services/agents/tools.py's `decide`)
must recompute exposure immediately, the same way services/review.py's
reviewer merge path already does (docs/plan.md §14c SUSPICIOUS 5b).

Four layers, four test groups:

1. Unit tests of services/er/persist.py's close_superseded_er_pairs against
   hand-built minimal scenarios: same-cluster closure (incl. the two-
   separate-decisions TRANSITIVE case), live hard-conflict closure, and a
   genuinely-unrelated pair that must stay open (never close a question
   that is not, in fact, moot).
2. Integration: services/review.py's reviewer merge path (tests/er_scenario
   .py's real gray band -- m4 vs m1/m2/m5/m3) closes the two items that
   become moot purely because m1/m2/m5 were already one pre-existing
   cluster, while the genuinely-different m4-vs-m3 pair stays open.
3. Integration: services/agents/tools.py's `decide` merge path both closes
   an otherwise-orphaned queued item AND recomputes exposure immediately --
   the recompute is verified over the real REST API (GET /persons/{id}),
   not just internal DB state, per the task's own verification bar.

Real :5434 Postgres throughout (UC2 convention); zero live LLM calls.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import get_settings
from app.db.models import (
    Document,
    IdentityLink,
    Mention,
    Passage,
    Person,
    PiiElement,
    ProcessingRun,
    ReviewItem,
)
from app.db.session import SessionLocal
from app.main import app
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.review import ReviewRepository
from app.services.agents.tools import ToolContext, run_tool
from app.services.er.persist import close_superseded_er_pairs, run_er_stage
from app.services.exposure import compute_exposure
from app.services.review import decide_review_item
from tests.agent_env import build_agent_env, teardown_agent_env
from tests.er_scenario import build_scenario, teardown_scenario

# ---------------------------------------------------------------------------
# Minimal hand-built scenario builders (own fixtures, no dependency on the
# other tests' private helpers -- kept self-contained deliberately).
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _mini_run(db) -> ProcessingRun:
    run = ProcessingRun(config_snapshot={"supersede_test": uuid.uuid4().hex}, status="finished")
    db.add(run)
    db.flush()
    return run


def _mini_document(db, run: ProcessingRun) -> Document:
    content = uuid.uuid4().hex.encode()
    document = Document(
        run_id=run.id,
        sha256=hashlib.sha256(content).hexdigest(),
        original_filename="x.txt",
        rel_path="x.txt",
        byte_size=len(content),
        file_class="txt",
        status="done",
    )
    db.add(document)
    db.flush()
    return document


def _mini_passage(db, document: Document) -> Passage:
    passage = Passage(document_id=document.id, seq=0, kind="text_block", locator={}, text="x")
    db.add(passage)
    db.flush()
    return passage


def _mini_mention(db, document: Document, passage: Passage, name: str) -> Mention:
    mention = Mention(
        document_id=document.id,
        passage_id=passage.id,
        name_raw=name,
        name_normalized=name.lower(),
        detector="llm_tier1",
        confidence=0.9,
    )
    db.add(mention)
    db.flush()
    return mention


def _mini_person(db, run: ProcessingRun, name: str) -> Person:
    person = Person(run_id=run.id, best_name=name, review_status="auto")
    db.add(person)
    db.flush()
    return person


def _link(db, person: Person, mention: Mention) -> IdentityLink:
    link = IdentityLink(
        person_id=person.id, mention_id=mention.id, score=0.9, method="rule",
        rule_id="test_setup", active=True,
    )
    db.add(link)
    db.flush()
    return link


def _move_link(db, mention: Mention, target: Person) -> None:
    """Simulate the append-only flip+insert a real merge performs (services/
    review.py's _merge_persons, services/agents/tools.py's _perform_merge)."""
    links_repo = IdentityLinkRepository(db)
    current = links_repo.active_link_for_mention(mention.id)
    if current is not None:
        links_repo.deactivate(current)
    links_repo.create(person_id=target.id, mention_id=mention.id, method="reviewer", rule_id="test_setup")


def _add_ssn_element(db, document: Document, passage: Passage, mention: Mention, ssn: str) -> PiiElement:
    element = PiiElement(
        document_id=document.id,
        passage_id=passage.id,
        mention_id=mention.id,
        element_type="ssn",
        value_raw=ssn,
        value_normalized=ssn.replace("-", ""),
        char_start=0,
        char_end=len(ssn),
        detector="checksum",
        validation_status="valid",
    )
    db.add(element)
    db.flush()
    return element


def _open_item(db, run: ProcessingRun, left: Mention, right: Mention, score: float = 0.5) -> ReviewItem:
    return ReviewRepository(db).create_item(
        kind="er_pair",
        ref={
            "run_id": str(run.id),
            "left_mention_id": str(left.id),
            "right_mention_id": str(right.id),
            "score": score,
        },
        reason="er_gray_band",
    )


def _teardown_mini_run(db, run: ProcessingRun) -> None:
    db.rollback()
    db.execute(delete(ReviewItem).where(ReviewItem.ref["run_id"].astext == str(run.id)))
    db.execute(delete(ProcessingRun).where(ProcessingRun.id == run.id))
    db.commit()


# ---------------------------------------------------------------------------
# 1. Unit tests of close_superseded_er_pairs
# ---------------------------------------------------------------------------


def test_supersede_closes_pair_already_in_same_cluster(db):
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_a = _mini_mention(db, doc, passage, "Alex A")
    m_b = _mini_mention(db, doc, passage, "Alex B")
    person = _mini_person(db, run, "Alex A")
    _link(db, person, m_a)
    _link(db, person, m_b)  # already one cluster BEFORE any decision
    item = _open_item(db, run, m_a, m_b)
    db.commit()
    try:
        closed = close_superseded_er_pairs(db, run.id)
        db.commit()

        assert closed == 1
        after = db.get(ReviewItem, item.id)
        assert after.status == "dismissed"
        decisions = ReviewRepository(db).decisions_for_item(item.id)
        assert len(decisions) == 1
        assert decisions[0].decision == "superseded"
        assert decisions[0].reviewer == "system"
        assert "same_cluster" in decisions[0].notes
    finally:
        _teardown_mini_run(db, run)


def test_supersede_transitive_via_two_separate_decisions(db):
    """A, B, C start as three separate singleton persons with all three
    pairwise pairs queued. Decision 1 merges (A,B). Decision 2 (separately)
    merges (B,C). Only AFTER decision 2 do A, B, and C all share one
    cluster -- the third, NEVER-directly-decided pair (A,C) must close then,
    and not a step earlier."""
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_a = _mini_mention(db, doc, passage, "Alex A")
    m_b = _mini_mention(db, doc, passage, "Alex B")
    m_c = _mini_mention(db, doc, passage, "Alex C")
    p_a = _mini_person(db, run, "Alex A")
    p_b = _mini_person(db, run, "Alex B")
    p_c = _mini_person(db, run, "Alex C")
    _link(db, p_a, m_a)
    _link(db, p_b, m_b)
    _link(db, p_c, m_c)
    item_ab = _open_item(db, run, m_a, m_b)
    item_bc = _open_item(db, run, m_b, m_c)
    item_ac = _open_item(db, run, m_a, m_c)
    db.commit()
    try:
        # Decision 1: merge A and B onto p_a. Mirror the REAL caller
        # (services/review.py's decide_review_item): the sweep only ever
        # sees OTHER open items, and the item actually being decided is
        # flipped to 'decided' by the caller right afterward -- replicate
        # that here so item_ab does not linger 'open' for decision 2's
        # sweep to (correctly, but confusingly for this test) rediscover.
        _move_link(db, m_b, p_a)
        db.commit()
        closed_1 = close_superseded_er_pairs(db, run.id, exclude_item_id=item_ab.id)
        item_ab.status = "decided"
        db.commit()
        assert closed_1 == 0  # C is not yet part of the cluster -- neither
        # (A,C) nor (B,C) is answered yet.
        assert db.get(ReviewItem, item_ac.id).status == "open"
        assert db.get(ReviewItem, item_bc.id).status == "open"

        # Decision 2: merge B (now on p_a) and C onto p_a.
        _move_link(db, m_c, p_a)
        db.commit()
        closed_2 = close_superseded_er_pairs(db, run.id, exclude_item_id=item_bc.id)
        db.commit()

        # (A,C) was never itself decided -- it closes purely because A, B,
        # and C transitively landed in one cluster across the two SEPARATE
        # decisions above.
        assert closed_2 == 1
        after_ac = db.get(ReviewItem, item_ac.id)
        assert after_ac.status == "dismissed"
        decisions = ReviewRepository(db).decisions_for_item(item_ac.id)
        assert decisions[-1].decision == "superseded"
        assert decisions[-1].reviewer == "system"
        assert "same_cluster" in decisions[-1].notes

        # item_bc was excluded (it is "the one just decided" in this call);
        # a real caller flips it to 'decided' itself right afterward.
        assert db.get(ReviewItem, item_bc.id).status == "open"
    finally:
        _teardown_mini_run(db, run)


def test_supersede_closes_live_hard_conflict_leaves_unrelated_pair_open(db):
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_x = _mini_mention(db, doc, passage, "Sam X")
    m_y = _mini_mention(db, doc, passage, "Sam Y")
    m_z = _mini_mention(db, doc, passage, "Sam Z")  # unrelated third mention
    _add_ssn_element(db, doc, passage, m_x, "111-22-3333")
    _add_ssn_element(db, doc, passage, m_y, "244-55-6666")  # conflicting SSN
    p_x = _mini_person(db, run, "Sam X")
    p_y = _mini_person(db, run, "Sam Y")
    p_z = _mini_person(db, run, "Sam Z")
    _link(db, p_x, m_x)
    _link(db, p_y, m_y)
    _link(db, p_z, m_z)
    item_conflict = _open_item(db, run, m_x, m_y)
    item_unrelated = _open_item(db, run, m_x, m_z)
    db.commit()
    try:
        closed = close_superseded_er_pairs(db, run.id)
        db.commit()

        assert closed == 1
        after_conflict = db.get(ReviewItem, item_conflict.id)
        assert after_conflict.status == "dismissed"
        decisions = ReviewRepository(db).decisions_for_item(item_conflict.id)
        assert "hard_conflict" in decisions[-1].notes

        # A pair with no evidence in common is genuinely undecided --
        # never silently closed.
        after_unrelated = db.get(ReviewItem, item_unrelated.id)
        assert after_unrelated.status == "open"
    finally:
        _teardown_mini_run(db, run)


def test_supersede_excludes_the_given_item_id(db):
    """The exact item the caller is in the middle of deciding must never be
    raced into 'dismissed' by its own decision's sweep (services/review.py
    relies on this)."""
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_a = _mini_mention(db, doc, passage, "Alex A")
    m_b = _mini_mention(db, doc, passage, "Alex B")
    person = _mini_person(db, run, "Alex A")
    _link(db, person, m_a)
    _link(db, person, m_b)
    item = _open_item(db, run, m_a, m_b)
    db.commit()
    try:
        closed = close_superseded_er_pairs(db, run.id, exclude_item_id=item.id)
        db.commit()
        assert closed == 0
        assert db.get(ReviewItem, item.id).status == "open"
    finally:
        _teardown_mini_run(db, run)


# ---------------------------------------------------------------------------
# 2. Integration: services/review.py's reviewer merge path
# ---------------------------------------------------------------------------


@pytest.fixture()
def scenario_ready():
    db = SessionLocal()
    scenario = build_scenario(db)
    run_er_stage(db, scenario.run_id)
    compute_exposure(db, scenario.run_id)
    db.commit()
    try:
        yield db, scenario
    finally:
        teardown_scenario(db, scenario)
        db.close()


def _item_for_pair(db, run_id, a, b) -> ReviewItem:
    pair = {str(a), str(b)}
    items, _ = ReviewRepository(db).list_items(kind="er_pair", status="open", run_id=run_id)
    for item in items:
        if {item.ref["left_mention_id"], item.ref["right_mention_id"]} == pair:
            return item
    raise AssertionError(f"no open er_pair item for {a} vs {b}")


def test_reviewer_merge_closes_transitively_moot_gray_items(scenario_ready):
    """tests/er_scenario.py's real gray band: m4 is queued against ALL of
    m1, m2, m5 (already one auto-linked "Robert Fournier" cluster) and m3
    (a genuinely different SharedName identity) -- 4 separate items over
    what is, from a person's-eye view, only two real questions. Resolving
    (m4, m1) must close (m4, m2) and (m4, m5) for free (same_cluster) and
    must NOT touch (m4, m3)."""
    db, scenario = scenario_ready
    m1, m2, m5 = scenario.mentions["m1"], scenario.mentions["m2"], scenario.mentions["m5"]
    m3, m4 = scenario.mentions["m3"], scenario.mentions["m4"]

    item_m4_m1 = _item_for_pair(db, scenario.run_id, m4, m1)
    item_m4_m2 = _item_for_pair(db, scenario.run_id, m4, m2)
    item_m4_m5 = _item_for_pair(db, scenario.run_id, m4, m5)
    item_m4_m3 = _item_for_pair(db, scenario.run_id, m4, m3)

    outcome = decide_review_item(
        db, item_m4_m1.id, decision="merge", reviewer="tester", notes="nickname + dob match"
    )
    db.commit()

    assert outcome.detail.get("superseded_items") == 2

    for item_id in (item_m4_m2.id, item_m4_m5.id):
        after = db.get(ReviewItem, item_id)
        assert after.status == "dismissed"
        decisions = ReviewRepository(db).decisions_for_item(item_id)
        assert decisions[-1].decision == "superseded"
        assert decisions[-1].reviewer == "system"

    # m4 vs m3 is a genuinely different identity (SharedName, different
    # SSN) -- must stay open for the adjudicator, never swept up.
    assert db.get(ReviewItem, item_m4_m3.id).status == "open"

    # the pair actually decided carries the real reviewer verdict, not a
    # system supersede -- decided by decide_review_item itself, not by
    # close_superseded_er_pairs (which this call excludes it from).
    decided = db.get(ReviewItem, item_m4_m1.id)
    assert decided.status == "decided"
    decided_decisions = ReviewRepository(db).decisions_for_item(item_m4_m1.id)
    assert decided_decisions[-1].decision == "merge"
    assert decided_decisions[-1].reviewer == "tester"


# ---------------------------------------------------------------------------
# 3. Integration: services/agents/tools.py's `decide` merge path
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent_ready():
    db = SessionLocal()
    environment = build_agent_env(db)
    try:
        yield db, environment
    finally:
        teardown_agent_env(db, environment)
        db.close()


def test_agent_merge_refreshes_absorbed_person_counts(agent_ready):
    """docs/plan.md §14c SUSPICIOUS 5b explicitly names doc-counts: the
    ABSORBED side of a merge must have its mention/document counts
    refreshed too, not just the survivor's. Pre-link env.m_bob and
    env.m_robert onto two DIFFERENT pre-existing persons (so `decide`'s
    merge takes the real absorption branch, not the "both sides already
    unlinked" shortcut every other test here uses) -- before this fix,
    _perform_merge only ever called _refresh_person_counts on the
    survivor, leaving the absorbed person's counts stuck at their
    pre-merge values even though every one of its active links just moved
    away."""
    db, env = agent_ready
    person_bob = Person(
        run_id=env.run.id, best_name="Bob Smith", review_status="auto",
        mention_count=1, document_count=1,
    )
    person_robert = Person(
        run_id=env.run.id, best_name="Robert Smith", review_status="auto",
        mention_count=1, document_count=1,
    )
    db.add_all([person_bob, person_robert])
    db.flush()
    db.add(
        IdentityLink(
            person_id=person_bob.id, mention_id=env.m_bob.id, score=0.9,
            method="rule", rule_id="test_setup", active=True,
        )
    )
    db.add(
        IdentityLink(
            person_id=person_robert.id, mention_id=env.m_robert.id, score=0.9,
            method="rule", rule_id="test_setup", active=True,
        )
    )
    db.commit()

    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_bob.id),
            "right_mention_id": str(env.m_robert.id),
            "decision": "merge",
            "rationale": "shared exact SSN plus nickname-compatible given names",
        },
        ToolContext(),
    )
    db.commit()
    assert not result.is_error
    survivor_id = uuid.UUID(result.payload["person_id"])
    assert survivor_id == person_bob.id  # left's person wins (services/agents/tools.py convention)
    assert result.payload["mentions_moved"] == 1

    db.refresh(person_bob)
    db.refresh(person_robert)
    assert person_bob.mention_count == 2
    assert person_bob.document_count == 2  # m_bob is doc_b, m_robert is doc_a
    # The fix under test: the absorbed person's counts reflect its
    # now-empty active-link set immediately, not the pre-merge snapshot.
    assert person_robert.mention_count == 0
    assert person_robert.document_count == 0


def test_agent_merge_closes_moot_item_and_recomputes_exposure_via_api(agent_ready):
    """env.m_bob and env.m_robert share an SSN (a legal, corroborated
    merge) but start with NO active links. Pre-link m_bob onto an
    already-existing third person (simulating an earlier decision) that
    also holds a third, unrelated mention -- then queue an er_pair item
    pairing env.m_robert against that third mention. Once `decide` merges
    m_bob+m_robert, m_robert joins the SAME person as the third mention,
    so that separately-queued item becomes moot (task 2) -- and the
    survivor's exposure flags must already be correct over the REST API
    with no separate manual compute_exposure call (task 3)."""
    db, env = agent_ready

    extra_person = Person(run_id=env.run.id, best_name="Bobby Smith", review_status="auto")
    db.add(extra_person)
    db.flush()
    extra_mention = Mention(
        document_id=env.doc_a.id,
        passage_id=env.passage_a.id,
        name_raw="Bobby Smith",
        name_normalized="bobby smith",
        detector="llm_tier1",
        confidence=0.9,
    )
    db.add(extra_mention)
    db.flush()
    db.add(
        IdentityLink(
            person_id=extra_person.id, mention_id=extra_mention.id, score=0.9,
            method="rule", rule_id="test_setup", active=True,
        )
    )
    # Simulate "m_bob was already merged onto extra_person by an earlier
    # decision" -- this is what makes the LATER (m_bob, m_robert) merge
    # transitively fold m_robert in with extra_mention too.
    db.add(
        IdentityLink(
            person_id=extra_person.id, mention_id=env.m_bob.id, score=0.9,
            method="rule", rule_id="test_setup", active=True,
        )
    )
    db.flush()
    moot_item = ReviewRepository(db).create_item(
        kind="er_pair",
        ref={
            "run_id": str(env.run.id),
            "left_mention_id": str(env.m_robert.id),
            "right_mention_id": str(extra_mention.id),
            "score": 0.5,
        },
        reason="er_gray_band",
    )
    db.commit()

    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_bob.id),
            "right_mention_id": str(env.m_robert.id),
            "decision": "merge",
            "rationale": "shared exact SSN plus nickname-compatible given names",
        },
        ToolContext(),
    )
    db.commit()
    assert not result.is_error
    merged_person_id = uuid.UUID(result.payload["person_id"])
    assert merged_person_id == extra_person.id  # m_bob's pre-existing person survives

    # --- task 2: the OTHER, separately-queued item became moot and closed.
    after_moot = db.get(ReviewItem, moot_item.id)
    assert after_moot.status == "dismissed"
    decisions = ReviewRepository(db).decisions_for_item(moot_item.id)
    assert decisions[-1].decision == "superseded"
    assert decisions[-1].reviewer == "system"

    # doc-counts corrected on both sides of the merge (docs/plan.md §14c
    # SUSPICIOUS 5b explicitly names doc-counts, not just flags).
    db.refresh(extra_person)
    assert extra_person.mention_count == 3  # m_bob + m_robert + extra_mention

    # --- task 3: exposure recomputed immediately, verified over the REAL
    # REST API (not just internal DB state) -- before this fix, nothing
    # called compute_exposure after an agent merge, so this endpoint would
    # have served an empty `flags: []` until a separate manual call.
    client = TestClient(app)
    headers = {"X-API-Key": get_settings().api_key}
    response = client.get(f"/api/v1/persons/{merged_person_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    categories = {f["category"] for f in body["flags"]}
    assert "ssn" in categories
    assert all(f["evidence_count"] >= 1 for f in body["flags"])
    ssn_flag = next(f for f in body["flags"] if f["category"] == "ssn")
    # both m_bob's and m_robert's SSN elements are now evidence for the
    # same flag -- the merge really did fold both mentions' PII together.
    assert ssn_flag["evidence_count"] == 2


# ---------------------------------------------------------------------------
# 4. Integration: `decide`'s no_merge/escalate outcomes also retire their
# own queued item -- found live during this integration pass (three real
# gray-band pairs dispatched through the adjudicator against a fresh
# corpus-mini run; the no_merge pair's own review_items row stayed 'open'
# forever, since close_superseded_er_pairs only detects a resolved question
# through same_cluster/hard_conflict side effects, and no_merge produces
# neither). Fixed in services/er/persist.py (find_open_er_pair_item,
# close_decided_er_pair_item) + services/agents/tools.py's `decide`.
# Escalate's item-creation had a second, related bug fixed alongside it:
# it unconditionally created a NEW item every time (never reusing one
# already open for the same pair) and that item's ref omitted `run_id`
# entirely, making it invisible to every run-scoped
# GET /review/items?run_id=... query -- including this very fix's own
# find_open_er_pair_item lookup on a later re-dispatch.
# ---------------------------------------------------------------------------


def test_agent_no_merge_closes_its_own_queued_item(db):
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_a = _mini_mention(db, doc, passage, "Pat Nolan")
    m_b = _mini_mention(db, doc, passage, "Patricia Nolan")
    item = _open_item(db, run, m_a, m_b, score=0.55)
    db.commit()
    try:
        result = run_tool(
            db,
            "decide",
            {
                "left_mention_id": str(m_a.id),
                "right_mention_id": str(m_b.id),
                "decision": "no_merge",
                "rationale": "name variant only, nothing corroborates it -- distinct people",
            },
            ToolContext(),
        )
        db.commit()
        assert not result.is_error
        assert result.payload["closed_review_item_id"] == str(item.id)

        after = db.get(ReviewItem, item.id)
        assert after.status == "decided"
        decisions = ReviewRepository(db).decisions_for_item(item.id)
        assert decisions[-1].decision == "no_merge"
        assert decisions[-1].reviewer == "agent"

        # The item no longer surfaces in the open queue -- a re-dispatch
        # would not find (and re-spend budget re-answering) it.
        open_items, _ = ReviewRepository(db).list_items(
            kind="er_pair", status="open", run_id=run.id
        )
        assert item.id not in {i.id for i in open_items}
    finally:
        _teardown_mini_run(db, run)


def test_agent_escalate_reuses_existing_open_item_without_duplicating(db):
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_a = _mini_mention(db, doc, passage, "Jordan Reyes")
    m_b = _mini_mention(db, doc, passage, "J. Reyes")
    item = _open_item(db, run, m_a, m_b, score=0.5)
    db.commit()
    try:
        result = run_tool(
            db,
            "decide",
            {
                "left_mention_id": str(m_a.id),
                "right_mention_id": str(m_b.id),
                "decision": "escalate",
                "rationale": "evidence genuinely balanced -- deferring to a human",
            },
            ToolContext(),
        )
        db.commit()
        assert not result.is_error
        assert result.payload["review_item_id"] == str(item.id)
        assert result.payload["review_item_reused"] is True

        # Still open (escalate defers, it does not answer) -- and no SECOND
        # item was created for the same question.
        after = db.get(ReviewItem, item.id)
        assert after.status == "open"
        _, total = ReviewRepository(db).list_items(kind="er_pair", status="open", run_id=run.id)
        assert total == 1
    finally:
        _teardown_mini_run(db, run)


def test_agent_escalate_without_existing_item_creates_run_scoped_one(db):
    """No pre-existing item (an ad hoc adjudicate_pair dispatch outside the
    review queue): escalate still creates one -- but now WITH run_id in its
    ref, so it is actually visible to the same run-scoped query a later
    dispatch pass (or the Review page) uses. Before the fix this ref had no
    run_id key at all."""
    run = _mini_run(db)
    doc = _mini_document(db, run)
    passage = _mini_passage(db, doc)
    m_a = _mini_mention(db, doc, passage, "Casey Blake")
    m_b = _mini_mention(db, doc, passage, "C. Blake")
    db.commit()
    try:
        result = run_tool(
            db,
            "decide",
            {
                "left_mention_id": str(m_a.id),
                "right_mention_id": str(m_b.id),
                "decision": "escalate",
                "rationale": "evidence genuinely balanced -- deferring to a human",
            },
            ToolContext(),
        )
        db.commit()
        assert not result.is_error
        new_item_id = uuid.UUID(result.payload["review_item_id"])
        assert "review_item_reused" not in result.payload

        created = db.get(ReviewItem, new_item_id)
        assert created.ref["run_id"] == str(run.id)

        scoped_items, _ = ReviewRepository(db).list_items(
            kind="er_pair", status="open", run_id=run.id
        )
        assert new_item_id in {i.id for i in scoped_items}
    finally:
        _teardown_mini_run(db, run)
