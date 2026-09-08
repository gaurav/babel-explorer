"""Unit tests for graph construction and the local viewer HTTP API."""

import json
import threading
import time
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import urlopen

from babel_explorer.core.babel_xrefs import LabeledCrossReference
from babel_explorer.core.nodenorm import Identifier
from babel_explorer.viewer import (
    ViewerService,
    create_viewer_server,
    graph_payload,
    provenance_source,
    read_static_asset,
)


def normalization(canonical, label, identifiers):
    return {
        "id": {"identifier": canonical, "label": label},
        "equivalent_identifiers": [
            {"identifier": identifier, "type": "biolink:Disease"}
            for identifier in identifiers
        ],
    }


def labeled_xref(filename, subj, obj, subj_label="", obj_label=""):
    return LabeledCrossReference(
        filename=filename,
        subj=subj,
        pred="oio:exactMatch",
        obj=obj,
        subj_label=subj_label,
        subj_biolink_type=("biolink:Disease",),
        obj_label=obj_label,
        obj_biolink_type=("biolink:Disease",),
    )


def test_provenance_source_uses_last_path_component():
    assert provenance_source("/runs/concords/MONDO") == "MONDO"
    assert provenance_source("/runs/concords/UMLS.parquet") == "UMLS"
    assert provenance_source("plain-source") == "plain-source"


def test_graph_payload_preserves_edges_sources_and_depths():
    payload = graph_payload(
        "A:1",
        [
            labeled_xref("/concords/MONDO", "A:1", "B:2", "Alpha", "Beta"),
            labeled_xref("/concords/UMLS", "B:2", "C:3", "Beta", "Gamma"),
        ],
        "2026jul22",
    )

    assert payload["query"] == "A:1"
    assert payload["babel_version"] == "2026jul22"
    assert len(payload["edges"]) == 2
    assert payload["edges"][0]["source"] == "MONDO"
    assert payload["edges"][0]["provenance"] == "/concords/MONDO"
    assert payload["sources"] == [
        {"name": "MONDO", "edge_count": 1, "node_count": 2},
        {"name": "UMLS", "edge_count": 1, "node_count": 2},
    ]
    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["A:1"]["query"] is True
    assert nodes["A:1"]["depth"] == 0
    assert nodes["B:2"]["depth"] == 1
    assert nodes["C:3"]["depth"] == 2
    assert nodes["B:2"]["degree"] == 2
    assert nodes["C:3"]["label"] == "Gamma"


def test_graph_payload_marks_query_and_external_cliques():
    query_normalization = normalization("A:1", "Alpha", ["A:1", "B:2"])
    payload = graph_payload(
        "A:1",
        [
            labeled_xref("/concords/MONDO", "A:1", "B:2", "Alpha", "Beta"),
            labeled_xref("/concords/UMLS", "B:2", "C:3", "Beta", "Gamma"),
        ],
        "2026jul22",
        query_normalization=query_normalization,
        normalizations={
            "A:1": query_normalization,
            "B:2": query_normalization,
            "C:3": normalization("C:3", "Gamma", ["C:3"]),
        },
    )

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["A:1"]["in_query_clique"] is True
    assert nodes["A:1"]["clique_leader"] is True
    assert nodes["B:2"]["in_query_clique"] is True
    assert nodes["B:2"]["clique_leader"] is False
    assert nodes["C:3"]["in_query_clique"] is False
    assert nodes["C:3"]["clique_leader"] is True
    assert payload["edges"][0]["within_query_clique"] is True
    assert payload["edges"][0]["cross_clique"] is False
    assert payload["edges"][1]["within_query_clique"] is False
    assert payload["edges"][1]["cross_clique"] is True


def test_graph_payload_matches_curie_prefix_case_for_clique_membership():
    query_normalization = normalization(
        "MONDO:0004979", "asthma", ["MONDO:0004979", "medgen:2109"]
    )
    payload = graph_payload(
        "MONDO:0004979",
        [],
        "2026jul22",
        mode="clique",
        query_normalization=query_normalization,
        seed_identifiers=[
            Identifier(curie="MONDO:0004979", label="asthma"),
            Identifier(curie="MEDGEN:2109"),
        ],
    )

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["MEDGEN:2109"]["in_query_clique"] is True
    assert nodes["MEDGEN:2109"]["clique_id"] == "MONDO:0004979"


def test_graph_payload_keeps_unknown_query_as_an_isolated_node():
    payload = graph_payload("UNKNOWN:1", [], None)
    assert payload["edges"] == []
    assert payload["sources"] == []
    assert payload["nodes"] == [
        {
            "id": "UNKNOWN:1",
            "label": "",
            "types": [],
            "degree": 0,
            "query": True,
            "clique_id": None,
            "clique_label": "",
            "clique_leader": False,
            "in_query_clique": True,
            "depth": 0,
        }
    ]


def test_viewer_service_defaults_to_query_clique():
    babel_xrefs = Mock()
    query_normalization = normalization(
        "MONDO:0004979",
        "asthma",
        ["MONDO:0004979", "DOID:2841"],
    )
    babel_xrefs.nodenorm.normalize_curie.return_value = query_normalization
    babel_xrefs.get_curie_xrefs.return_value = [
        labeled_xref("/concords/MONDO", "MONDO:0004979", "DOID:2841"),
        labeled_xref("/concords/MONDO", "MONDO:0004979", "UMLS:C0004096"),
    ]
    service = ViewerService(babel_xrefs, Mock(), "2026jul22")

    payload = service.get_graph("MONDO:0004979")

    babel_xrefs.get_curie_xrefs.assert_called_once_with(
        ["DOID:2841", "MONDO:0004979"], recurse=False, label_curies=True
    )
    assert payload["query"] == "MONDO:0004979"
    assert payload["mode"] == "clique"
    assert {node["id"] for node in payload["nodes"]} == {
        "MONDO:0004979",
        "DOID:2841",
    }
    assert len(payload["edges"]) == 1
    service.close()


def test_viewer_service_runs_recursive_labeled_query():
    babel_xrefs = Mock()
    query_normalization = normalization("MONDO:0004979", "asthma", ["MONDO:0004979"])
    babel_xrefs.nodenorm.normalize_curie.return_value = query_normalization
    babel_xrefs.get_curie_xrefs.return_value = []
    service = ViewerService(babel_xrefs, Mock(), "2026jul22")

    payload = service.get_graph("MONDO:0004979", recurse=True)

    babel_xrefs.get_curie_xrefs.assert_called_once_with(
        ["MONDO:0004979"], recurse=True, label_curies=True
    )
    assert payload["mode"] == "recursive"
    service.close()


def test_viewer_service_runs_graph_as_pollable_job():
    babel_xrefs = Mock()
    babel_xrefs.nodenorm.normalize_curie.return_value = normalization(
        "MONDO:0004979", "asthma", ["MONDO:0004979"]
    )
    babel_xrefs.get_curie_xrefs.return_value = []
    service = ViewerService(babel_xrefs, Mock(), "2026jul22")
    try:
        job = service.start_graph("MONDO:0004979")
        deadline = time.monotonic() + 2
        while job["status"] != "complete" and time.monotonic() < deadline:
            time.sleep(0.01)
            job = service.get_graph_job(job["job_id"])
    finally:
        service.close()

    assert job["status"] == "complete"
    assert job["recurse"] is False
    assert job["graph"]["query"] == "MONDO:0004979"


class StubViewerService:
    def resolve_name(self, query):
        return [{"curie": "MONDO:0004979", "label": query}]

    def start_graph(self, curie, recurse=False):
        return {
            "job_id": "job-1",
            "curie": curie,
            "recurse": recurse,
            "status": "queued",
        }

    def get_graph_job(self, job_id):
        return {
            "job_id": job_id,
            "curie": "MONDO:0004979",
            "recurse": False,
            "status": "complete",
            "graph": {
                "query": "MONDO:0004979",
                "mode": "clique",
                "nodes": [],
                "edges": [],
                "sources": [],
                "cliques": [],
            },
        }


def request_json(base_url, path):
    with urlopen(base_url + path) as response:
        return response.status, json.load(response)


def test_viewer_http_routes():
    server = create_viewer_server(StubViewerService(), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base_url + "/") as response:
            assert response.status == 200
            assert b"Babel Explorer" in response.read()

        status, payload = request_json(base_url, "/api/resolve?query=asthma")
        assert status == 200
        assert payload["results"][0]["curie"] == "MONDO:0004979"

        status, payload = request_json(base_url, "/api/graph?curie=MONDO%3A0004979")
        assert status == 202
        assert payload["status"] == "queued"
        assert payload["recurse"] is False

        status, payload = request_json(
            base_url, "/api/graph?curie=MONDO%3A0004979&recurse=true"
        )
        assert status == 202
        assert payload["recurse"] is True

        status, payload = request_json(base_url, "/api/graph-status?job_id=job-1")
        assert status == 200
        assert payload["graph"]["query"] == "MONDO:0004979"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_viewer_http_rejects_short_name_query():
    server = create_viewer_server(StubViewerService(), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/resolve?query=a"):
            raise AssertionError("Expected HTTP 400")
    except HTTPError as error:
        assert error.code == 400
        assert "at least two characters" in json.load(error)["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_packaged_static_assets_are_available():
    assert b"Babel Explorer" in read_static_asset("index.html")
    assert b"loadGraph" in read_static_asset("app.js")
    assert b".graph-stage" in read_static_asset("styles.css")
