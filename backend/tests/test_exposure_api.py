"""API tests: GET /exposure filters (SQL-side search/category/review_status/
min_confidence + pagination), GET /persons/{id} drill-down (evidence refs +
ER panel), GET /review/items resolved refs, and auth. Over the fixture
scenario, through the real app with TestClient (UC2 convention)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.services.er.persist import run_er_stage
from app.services.exposure import compute_exposure
from tests.er_scenario import build_scenario, teardown_scenario

HEADERS = {"X-API-Key": get_settings().api_key}


@pytest.fixture(scope="module")
def api():
    db = SessionLocal()
    scenario = build_scenario(db)
    run_er_stage(db, scenario.run_id)
    compute_exposure(db, scenario.run_id)
    db.commit()
    client = TestClient(app)
    try:
        yield client, scenario
    finally:
        teardown_scenario(db, scenario)
        db.close()


def _exposure(client, scenario, **params):
    params["run_id"] = str(scenario.run_id)
    response = client.get("/api/v1/exposure", params=params, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_requires_api_key(api):
    client, scenario = api
    assert client.get("/api/v1/exposure").status_code == 401
    assert client.get(f"/api/v1/exposure?run_id={scenario.run_id}").status_code == 401


def test_lists_persons_with_flags(api):
    client, scenario = api
    page = _exposure(client, scenario)
    assert page["total"] == 5
    # TWO persons print as "Robert Fournier" (SharedName pair, kept apart
    # by design); the real cluster is the 3-mention one.
    robert = next(
        p
        for p in page["items"]
        if p["best_name"] == "Robert Fournier" and p["mention_count"] == 3
    )
    categories = {f["category"] for f in robert["flags"]}
    assert categories == {"ssn", "dob", "credit_card", "credentials"}
    ssn_flag = next(f for f in robert["flags"] if f["category"] == "ssn")
    assert ssn_flag["exposed"] is True
    assert ssn_flag["evidence_count"] == 4
    assert {a["name"] for a in robert["aliases"]} == {"Bob Fournier"}
    assert robert["mention_count"] == 3


def test_search_matches_aliases(api):
    client, scenario = api
    page = _exposure(client, scenario, search="Bob")
    assert [p["best_name"] for p in page["items"]] == ["Robert Fournier"]
    page = _exposure(client, scenario, search="ellison")
    assert page["total"] == 2  # both Dana persons
    page = _exposure(client, scenario, search="zzz-no-such-person")
    assert page["total"] == 0


def test_category_and_confidence_filters(api):
    client, scenario = api
    page = _exposure(client, scenario, category="credentials")
    assert [p["best_name"] for p in page["items"]] == ["Robert Fournier"]
    # checksum-default confidence is 0.95: 0.9 passes, 0.99 filters out.
    assert _exposure(client, scenario, category="ssn", min_confidence=0.9)["total"] >= 1
    assert _exposure(client, scenario, category="ssn", min_confidence=0.99)["total"] == 0


def test_review_status_filter_and_pagination(api):
    client, scenario = api
    everything = _exposure(client, scenario)
    by_status = {}
    for person in everything["items"]:
        by_status.setdefault(person["review_status"], []).append(person)
    for status_value, persons in by_status.items():
        page = _exposure(client, scenario, review_status=status_value)
        assert page["total"] == len(persons)
    page = _exposure(client, scenario, limit=2, offset=0)
    assert len(page["items"]) == 2 and page["total"] == 5
    names_a = [p["id"] for p in page["items"]]
    page_b = _exposure(client, scenario, limit=2, offset=2)
    assert not set(names_a) & {p["id"] for p in page_b["items"]}


def test_person_detail_drilldown(api):
    client, scenario = api
    page = _exposure(client, scenario, search="Robert Fournier")
    person_id = next(p["id"] for p in page["items"] if p["mention_count"] == 3)
    response = client.get(f"/api/v1/persons/{person_id}", headers=HEADERS)
    assert response.status_code == 200
    detail = response.json()
    assert detail["best_name"] == "Robert Fournier"

    ssn_flag = next(f for f in detail["flags"] if f["category"] == "ssn")
    assert len(ssn_flag["evidence"]) == 4
    for evidence in ssn_flag["evidence"]:
        assert evidence["document_filename"]
        assert evidence["passage_id"]
        assert evidence["snippet"]
        assert evidence["char_start"] is not None and evidence["char_end"] is not None

    assert detail["identity_links"], "ER panel must list the links"
    for link in detail["identity_links"]:
        assert link["method"] in ("rule", "agent", "reviewer")
        assert link["rule_id"]
        assert link["rationale"]
        assert link["mention_name_raw"]

    assert client.get(f"/api/v1/persons/{uuid.uuid4()}", headers=HEADERS).status_code == 404


def test_review_items_endpoint_resolves_refs(api):
    client, scenario = api
    response = client.get(
        "/api/v1/review/items",
        params={"kind": "er_pair", "status": "open", "run_id": str(scenario.run_id)},
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 2
    item = body["items"][0]
    assert item["kind"] == "er_pair"
    resolved = item["resolved"]
    # Side-by-side evidence sets for the reviewer (docs/plan.md §7).
    for side in ("left", "right"):
        card = resolved[side]
        assert card["name_raw"]
        assert card["document_filename"]
        assert "elements" in card
    assert resolved["score"] == item["ref"]["score"]

    response = client.get(
        "/api/v1/review/items",
        params={"kind": "extraction", "status": "open", "run_id": str(scenario.run_id)},
        headers=HEADERS,
    )
    extraction = response.json()
    assert extraction["total"] == 1  # the orphan phone
    element_card = extraction["items"][0]["resolved"]["element"]
    assert element_card["element_type"] == "phone"
    assert element_card["document_filename"] == "orphan.txt"


def test_review_decision_via_api_round_trips(api):
    client, scenario = api
    listing = client.get(
        "/api/v1/review/items",
        params={"kind": "er_pair", "status": "open", "run_id": str(scenario.run_id)},
        headers=HEADERS,
    ).json()
    item = listing["items"][0]
    response = client.post(
        f"/api/v1/review/items/{item['id']}/decision",
        json={"decision": "keep_separate", "reviewer": "api-tester", "notes": "distinct"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "decided"
    assert body["decision"] == "keep_separate"

    # Race safety: the same decision again is a 409, never a double-apply.
    again = client.post(
        f"/api/v1/review/items/{item['id']}/decision",
        json={"decision": "merge", "reviewer": "api-tester-2"},
        headers=HEADERS,
    )
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "conflict"
