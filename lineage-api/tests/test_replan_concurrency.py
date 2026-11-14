"""Concurrency test: replan requests vs concurrent registrations.

Guarantee under test: every plan is computed from one complete committed
snapshot — either the lineage before a registration or after it — never a mix
of the two.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Two complete snapshots that give r1 different candidate groups.
BATCH_A = {
    "nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ]
}
BATCH_B = {
    "nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s2"]]},
    ]
}


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    db_file = tmp_path / "replan_concurrency.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(db_file))
    db.init_db(str(db_file))

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        pytest.fail("uvicorn server did not start")

    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


def test_replan_never_mixes_concurrent_snapshots(live_server):
    base_url = live_server
    resp = httpx.post(f"{base_url}/lineage/register", json=BATCH_A, timeout=10)
    assert resp.status_code == 201

    errors: list[Exception] = []
    stop = threading.Event()

    def writer():
        payloads = [BATCH_B, BATCH_A]
        i = 0
        while not stop.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/register",
                    json=payloads[i % 2],
                    timeout=15,
                )
                assert r.status_code == 201, r.text
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            i += 1
            time.sleep(0.002)

    def reader_replan():
        while not stop.is_set():
            try:
                # Costs cover the union of relevant non-source columns of
                # both snapshots; the unused entry must be reported as 422
                # only when it is not part of the observed snapshot.
                r = httpx.post(
                    f"{base_url}/lineage/replan",
                    json={
                        "targets": ["r1"],
                        "costs": {"d1": 10.0, "r1": 1.0},
                    },
                    timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    names = {c["name"] for c in data["columns"]}
                    # Snapshot A: r1 depends on d1 (must be executed).
                    # Snapshot B: r1 reads s2 directly (d1 absent entirely).
                    assert names in ({"d1", "r1"}, {"r1"})
                    if names == {"d1", "r1"}:
                        assert data["total_cost"] == 11.0
                        assert data["execution_order"] == ["d1", "r1"]
                    else:
                        assert data["total_cost"] == 1.0
                        assert data["execution_order"] == ["r1"]
                else:
                    # In snapshot B, d1 is not a registered node, so its cost
                    # entry is unexpected and the request must be rejected
                    # cleanly — never with a 5xx or an internally inconsistent
                    # plan.
                    assert r.status_code == 422, r.text
                    assert "d1" in r.json()["details"]["unexpected_costs"]
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=reader_replan, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(4)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, [repr(e) for e in errors[:5]]
