"""Functional tests for the minimum-cost recalculation plan API."""


def _register(client, nodes, expected=201):
    resp = client.post("/lineage/register", json={"nodes": nodes})
    assert resp.status_code == expected, resp.text
    return resp.json()


def _replan(client, targets, costs, expected=200):
    resp = client.post(
        "/lineage/replan", json={"targets": targets, "costs": costs}
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def _diamond_graph(client):
    # s -> d1, d2 -> d3 -> r1
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "d2", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "d3", "kind": "derived",
         "candidate_groups": [["d1", "d2"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d3"]]},
    ])


def test_basic_plan_costs_groups_and_topological_order(client):
    _diamond_graph(client)
    costs = {"d1": 2.0, "d2": 3.0, "d3": 4.0, "r1": 5.0}
    data = _replan(client, ["r1"], costs)

    assert data["targets"] == ["r1"]
    assert data["total_cost"] == 14.0
    by_name = {c["name"]: c for c in data["columns"]}
    assert set(by_name) == {"d1", "d2", "d3", "r1"}
    assert by_name["d1"]["group_index"] == 0
    assert by_name["d1"]["inputs"] == ["s1"]
    assert by_name["d3"]["inputs"] == ["d1", "d2"]
    assert by_name["r1"]["inputs"] == ["d3"]
    for name, col in by_name.items():
        assert col["cost"] == costs[name]
        assert col["kind"] in {"derived", "report"}

    # Every executed non-source input must appear earlier than its dependent.
    order = data["execution_order"]
    position = {name: i for i, name in enumerate(order)}
    assert order[0] in {"d1", "d2"}
    assert order[-1] == "r1"
    for col in data["columns"]:
        for inp in col["inputs"]:
            if inp in position:
                assert position[inp] < position[col["name"]]


def test_shared_derived_column_executed_once_with_one_group(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
        {"name": "r2", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    data = _replan(client, ["r1", "r2"], {"d1": 10.0, "r1": 1.0, "r2": 1.0})
    names = [c["name"] for c in data["columns"]]
    assert sorted(names) == ["d1", "r1", "r2"]
    # d1 is shared: exactly one entry, one chosen group.
    d1_entries = [c for c in data["columns"] if c["name"] == "d1"]
    assert len(d1_entries) == 1
    assert d1_entries[0]["group_index"] == 0
    # Cost of d1 counted only once.
    assert data["total_cost"] == 12.0
    assert data["execution_order"].index("d1") == 0


def test_cheapest_candidate_group_path_is_selected(client):
    # r1 can use the expensive derived d1 or read source s2 directly.
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["d1"], ["s2"]]},
    ])
    data = _replan(client, ["r1"], {"d1": 100.0, "r1": 1.0})
    names = {c["name"] for c in data["columns"]}
    assert names == {"r1"}  # expensive d1 avoided entirely
    r1 = data["columns"][0]
    assert r1["group_index"] == 1
    assert r1["inputs"] == ["s2"]
    assert data["total_cost"] == 1.0


def test_alternative_group_inside_derived_column_changes_closure(client):
    # d2 can be built from d1 (costs d1) or directly from s2 (avoids d1).
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "d2", "kind": "derived",
         "candidate_groups": [["d1"], ["s2"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d2"]]},
    ])
    data = _replan(client, ["r1"], {"d1": 50.0, "d2": 5.0, "r1": 1.0})
    names = {c["name"] for c in data["columns"]}
    assert names == {"d2", "r1"}
    by_name = {c["name"]: c for c in data["columns"]}
    assert by_name["d2"]["group_index"] == 1
    assert by_name["d2"]["inputs"] == ["s2"]
    assert data["total_cost"] == 6.0


def test_report_directly_on_sources_executes_only_report(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1", "s2"]]},
    ])
    data = _replan(client, ["r1"], {"r1": 7.5})
    assert [c["name"] for c in data["columns"]] == ["r1"]
    assert data["total_cost"] == 7.5
    assert data["execution_order"] == ["r1"]


def test_zero_costs_are_accepted(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    data = _replan(client, ["r1"], {"d1": 0, "r1": 0})
    assert data["total_cost"] == 0.0
    assert sorted(c["name"] for c in data["columns"]) == ["d1", "r1"]


def test_unknown_target_returns_404(client):
    _register(client, [{"name": "s1", "kind": "source"}])
    resp = client.post(
        "/lineage/replan",
        json={"targets": ["ghost"], "costs": {"ghost": 1.0}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert resp.json()["details"]["unknown"] == ["ghost"]


def test_source_target_and_derived_target_are_rejected(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
    ])
    resp = client.post(
        "/lineage/replan",
        json={"targets": ["s1"], "costs": {}},
    )
    assert resp.status_code == 422
    assert resp.json()["details"]["source_targets"] == ["s1"]

    resp = client.post(
        "/lineage/replan",
        json={"targets": ["d1"], "costs": {"d1": 1.0}},
    )
    assert resp.status_code == 422
    assert resp.json()["details"]["non_report_targets"] == ["d1"]


def test_duplicate_targets_are_rejected(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ])
    resp = client.post(
        "/lineage/replan",
        json={"targets": ["r1", "r1"], "costs": {"r1": 1.0}},
    )
    assert resp.status_code == 422
    assert resp.json()["details"]["duplicate_targets"] == ["r1"]


def test_missing_costs_are_rejected(client):
    _diamond_graph(client)
    resp = client.post(
        "/lineage/replan",
        json={"targets": ["r1"], "costs": {"d1": 1.0, "r1": 1.0}},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert body["details"]["missing_costs"] == ["d2", "d3"]


def test_source_and_unknown_cost_entries_are_rejected(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ])
    resp = client.post(
        "/lineage/replan",
        json={
            "targets": ["r1"],
            "costs": {"r1": 1.0, "s1": 2.0, "ghost": 3.0},
        },
    )
    assert resp.status_code == 422
    details = resp.json()["details"]
    assert details["unexpected_costs"] == ["ghost", "s1"]


def test_negative_nan_and_infinite_costs_are_rejected(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ])
    # NaN / Infinity are not strict JSON tokens; send them raw so the server's
    # pydantic validation (allow_inf_nan=False) is the layer that rejects them.
    raw_payloads = [
        '{"targets": ["r1"], "costs": {"r1": -1.0}}',
        '{"targets": ["r1"], "costs": {"r1": NaN}}',
        '{"targets": ["r1"], "costs": {"r1": Infinity}}',
        '{"targets": ["r1"], "costs": {"r1": -Infinity}}',
    ]
    for payload in raw_payloads:
        resp = client.post(
            "/lineage/replan",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, payload


def test_more_than_16_relevant_non_source_columns_rejected(client):
    nodes = [{"name": "s1", "kind": "source"}]
    previous = "s1"
    for i in range(1, 17):  # d1 .. d16
        name = f"d{i}"
        nodes.append(
            {"name": name, "kind": "derived",
             "candidate_groups": [[previous]]}
        )
        previous = name
    nodes.append(
        {"name": "r1", "kind": "report",
         "candidate_groups": [[previous]]}
    )
    _register(client, nodes)

    costs = {f"d{i}": 1.0 for i in range(1, 17)}
    costs["r1"] = 1.0
    resp = client.post(
        "/lineage/replan", json={"targets": ["r1"], "costs": costs}
    )
    assert resp.status_code == 422
    details = resp.json()["details"]
    assert details["limit"] == 16
    assert details["count"] == 17
    assert "r1" in details["relevant_non_source"]


def test_exactly_16_relevant_non_source_columns_accepted(client):
    nodes = [{"name": "s1", "kind": "source"}]
    previous = "s1"
    for i in range(1, 16):  # d1 .. d15
        name = f"d{i}"
        nodes.append(
            {"name": name, "kind": "derived",
             "candidate_groups": [[previous]]}
        )
        previous = name
    nodes.append(
        {"name": "r1", "kind": "report",
         "candidate_groups": [[previous]]}
    )
    _register(client, nodes)

    costs = {f"d{i}": 1.0 for i in range(1, 16)}
    costs["r1"] = 1.0
    data = _replan(client, ["r1"], costs)
    assert len(data["columns"]) == 16
    assert data["execution_order"][-1] == "r1"


def test_planning_does_not_modify_lineage(client):
    _diamond_graph(client)
    before = client.get("/lineage/graph").json()
    _replan(client, ["r1"], {"d1": 1.0, "d2": 1.0, "d3": 1.0, "r1": 1.0})
    # Rejected plans must not touch the lineage either.
    client.post(
        "/lineage/replan", json={"targets": ["nope"], "costs": {}}
    )
    after = client.get("/lineage/graph").json()
    assert after == before


def test_multiple_targets_partial_sharing_global_optimum(client):
    # r1 needs d1 and d2; r2 needs d2 and d3. d2 is shared and counted once.
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "d2", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "d3", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["d1", "d2"]]},
        {"name": "r2", "kind": "report",
         "candidate_groups": [["d2", "d3"]]},
    ])
    costs = {"d1": 2.0, "d2": 3.0, "d3": 4.0, "r1": 5.0, "r2": 6.0}
    data = _replan(client, ["r2", "r1"], costs)
    assert data["targets"] == ["r1", "r2"]
    names = sorted(c["name"] for c in data["columns"])
    assert names == ["d1", "d2", "d3", "r1", "r2"]
    assert data["total_cost"] == 20.0  # 2+3+4+5+6, d2 only once

    order = data["execution_order"]
    position = {name: i for i, name in enumerate(order)}
    by_name = {c["name"]: c for c in data["columns"]}
    for col in data["columns"]:
        for inp in col["inputs"]:
            if inp in position:
                assert position[inp] < position[col["name"]]
    assert position["r1"] > position["d1"]
    assert position["r2"] > position["d3"]
    assert by_name["d2"]["group_index"] == 0
