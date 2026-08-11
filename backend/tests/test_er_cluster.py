"""Unit tests for services/er/cluster.py: union-find correctness, the
three bands (docs/plan.md D5), gray-queue hygiene, and the transitive
hard-conflict guarantee -- the invariant §15's gate query checks at
corpus scale (SharedName pairs stay distinct)."""

from app.services.er.cluster import UnionFind, cluster_mentions
from app.services.er.features import compute_features
from app.services.er.normalize import build_mention
from app.services.er.scoring import score_pair


def scored(a, b):
    return score_pair(a.mention_id, b.mention_id, compute_features(a, b))


# --- union-find ------------------------------------------------------------


def test_union_find_transitivity():
    uf = UnionFind(["a", "b", "c", "d"])
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")
    assert uf.find("d") != uf.find("a")


def test_union_find_groups_are_deterministic_and_complete():
    uf = UnionFind(["e", "a", "c", "b", "d"])
    uf.union("d", "e")
    uf.union("b", "a")
    assert uf.groups() == [["a", "b"], ["c"], ["d", "e"]]


def test_union_find_idempotent_union():
    uf = UnionFind(["a", "b"])
    root1 = uf.union("a", "b")
    root2 = uf.union("a", "b")
    assert root1 == root2
    assert len(uf.groups()) == 1


def test_union_find_add_unknown_on_find():
    uf = UnionFind()
    assert uf.find("x") == "x"


# --- banding ---------------------------------------------------------------


def _mentions_abc():
    """a-b share an SSN (auto band); c shares only the name (name-only
    cap -> distinct band)."""
    a = build_mention("a", "D1", "Ana Cruz", elements=[("ssn", "111-22-3333")])
    b = build_mention("b", "D2", "Ana Cruz", elements=[("ssn", "111-22-3333")])
    c = build_mention("c", "D3", "Ana Cruz")
    return a, b, c


def test_auto_band_links_and_distinct_band_does_not():
    a, b, c = _mentions_abc()
    pairs = [scored(a, b), scored(a, c), scored(b, c)]
    result = cluster_mentions(["a", "b", "c"], pairs)
    assert result.clusters == [["a", "b"], ["c"]]
    assert len(result.auto_links) == 1
    assert result.gray_pairs == []  # name-only pairs are BELOW gray, not in it


def test_gray_band_pair_is_queued_not_linked():
    # Nickname variant without corroboration: gray band -> adjudicator.
    a = build_mention("a", "D1", "Robert Chen")
    b = build_mention("b", "D2", "Bob Chen")
    result = cluster_mentions(["a", "b"], [scored(a, b)])
    assert result.clusters == [["a"], ["b"]]
    assert len(result.gray_pairs) == 1
    assert result.gray_pairs[0].hard_reason == "name_variant_cap"


def test_gray_pair_already_implied_by_auto_links_is_dropped():
    """a-b and b-c auto-link; the gray a-c pair is then implied and must
    NOT waste adjudicator budget."""
    a = build_mention("a", "D1", "Robert Chen", elements=[("ssn", "111-22-3333")])
    b = build_mention(
        "b", "D2", "Robert Chen",
        elements=[("ssn", "111-22-3333"), ("phone", "913-555-0142")],
    )
    c = build_mention("c", "D3", "Bob Chen", elements=[("phone", "(913) 555-0142")])
    pairs = [scored(a, b), scored(b, c), scored(a, c)]
    ac = [p for p in pairs if {p.left_id, p.right_id} == {"a", "c"}][0]
    assert 0.45 < ac.score < 0.85  # genuinely gray on its own
    result = cluster_mentions(["a", "b", "c"], pairs)
    assert result.clusters == [["a", "b", "c"]]
    assert result.gray_pairs == []


def test_singletons_survive_clustering():
    # A mention no pair ever references still becomes its own cluster
    # (a person seen once is still a person in the exposure table).
    result = cluster_mentions(["lone"], [])
    assert result.clusters == [["lone"]]
    assert result.cluster_of == {"lone": "lone"}


def test_invalid_thresholds_raise():
    try:
        cluster_mentions([], [], auto_link_threshold=0.4, distinct_threshold=0.45)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for inverted thresholds")


# --- transitive hard-conflict guarantee ------------------------------------


def test_hard_conflict_blocks_transitive_merge():
    """a-b auto (shared email), b-c auto (shared phone), a-c hard SSN
    conflict: naive transitivity would put two different SSNs in one
    person. The conflict must win -- one union is refused and surfaced."""
    a = build_mention(
        "a", "D1", "Jordan Reyes",
        elements=[("ssn", "123-45-6789"), ("email", "j@example.com")],
    )
    b = build_mention(
        "b", "D2", "Jordan Reyes",
        elements=[("email", "j@example.com"), ("phone", "913-555-0142")],
    )
    c = build_mention(
        "c", "D3", "Jordan Reyes",
        elements=[("ssn", "987-65-4321"), ("phone", "913-555-0142")],
    )
    pairs = [scored(a, b), scored(b, c), scored(a, c)]
    result = cluster_mentions(["a", "b", "c"], pairs)
    roots = result.cluster_of
    assert roots["a"] != roots["c"], "conflicting SSNs ended up in one cluster"
    assert len(result.blocked_by_conflict) == 1


def test_direct_hard_conflict_pair_never_links():
    a = build_mention("a", "D1", "Jordan Reyes", elements=[("ssn", "123-45-6789")])
    b = build_mention("b", "D2", "Jordan Reyes", elements=[("ssn", "987-65-4321")])
    result = cluster_mentions(["a", "b"], [scored(a, b)])
    assert result.clusters == [["a"], ["b"]]
    assert result.gray_pairs == []


def test_clustering_is_deterministic_under_pair_order():
    a, b, c = _mentions_abc()
    d = build_mention("d", "D4", "Ana Cruz", elements=[("ssn", "111-22-3333")])
    mentions = [a, b, c, d]
    pairs = [
        scored(x, y)
        for i, x in enumerate(mentions)
        for y in mentions[i + 1 :]
    ]
    forward = cluster_mentions(["a", "b", "c", "d"], pairs)
    backward = cluster_mentions(["d", "c", "b", "a"], list(reversed(pairs)))
    assert forward.clusters == backward.clusters
    assert [
        (p.left_id, p.right_id) for p in forward.gray_pairs
    ] == [(p.left_id, p.right_id) for p in backward.gray_pairs]
