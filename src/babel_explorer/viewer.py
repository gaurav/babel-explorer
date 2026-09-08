"""Local HTTP application for exploring Babel cross-reference graphs."""

import dataclasses
import json
import logging
import os
import threading
import time
import uuid
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

import requests

from babel_explorer.core.babel_xrefs import LabeledCrossReference
from babel_explorer.core.nodenorm import Identifier

STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def provenance_source(filename: str) -> str:
    """Return the compact source name represented by a Babel provenance path."""
    source = os.path.basename(filename.rstrip("/")) or filename
    for suffix in (".parquet", ".tsv", ".csv", ".jsonl", ".txt"):
        if source.lower().endswith(suffix):
            return source[: -len(suffix)]
    return source


def curie_key(curie: str) -> str:
    """Return a comparison key with a case-insensitive CURIE prefix."""
    if curie.startswith(("http://", "https://")):
        return curie
    prefix, separator, local_id = curie.partition(":")
    if not separator:
        return curie
    return f"{prefix.upper()}:{local_id}"


def _node_depths(query_curie: str, edges: list[dict]) -> dict[str, int]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["subject"], set()).add(edge["object"])
        adjacency.setdefault(edge["object"], set()).add(edge["subject"])

    depths = {query_curie: 0}
    frontier = deque([query_curie])
    while frontier:
        current = frontier.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor not in depths:
                depths[neighbor] = depths[current] + 1
                frontier.append(neighbor)
    return depths


def graph_payload(
    query_curie: str,
    xrefs: list,
    babel_version: str | None,
    *,
    mode: str = "recursive",
    query_normalization: dict | None = None,
    normalizations: dict[str, dict | None] | None = None,
    seed_identifiers: list[Identifier] | None = None,
) -> dict:
    """Convert recursive labeled xrefs into the browser's node-link representation."""
    normalizations = normalizations or {}
    seed_identifiers = seed_identifiers or []
    query_key = curie_key(query_curie)
    nodes: dict[str, dict] = {}
    edges = []

    def add_node(curie: str, label: str, types) -> None:
        node = nodes.setdefault(
            curie,
            {
                "id": curie,
                "label": "",
                "types": set(),
                "degree": 0,
                "query": curie_key(curie) == query_key,
            },
        )
        if label and not node["label"]:
            node["label"] = label
        node["types"].update(types or ())

    for index, xref in enumerate(xrefs):
        labeled = isinstance(xref, LabeledCrossReference)
        add_node(
            xref.subj,
            xref.subj_label if labeled else "",
            xref.subj_biolink_type if labeled else (),
        )
        add_node(
            xref.obj,
            xref.obj_label if labeled else "",
            xref.obj_biolink_type if labeled else (),
        )
        nodes[xref.subj]["degree"] += 1
        nodes[xref.obj]["degree"] += 1
        edges.append(
            {
                "id": f"edge-{index}",
                "subject": xref.subj,
                "predicate": xref.pred,
                "object": xref.obj,
                "source": provenance_source(xref.filename),
                "provenance": xref.filename,
            }
        )

    nodes_by_key = {curie_key(curie): node for curie, node in nodes.items()}
    for identifier in seed_identifiers:
        key = curie_key(identifier.curie)
        if key in nodes_by_key:
            node = nodes_by_key[key]
            if identifier.label and not node["label"]:
                node["label"] = identifier.label
            node["types"].update(identifier.biolink_type)
            continue
        add_node(identifier.curie, identifier.label, identifier.biolink_type)
        nodes_by_key[key] = nodes[identifier.curie]

    if query_key not in nodes_by_key:
        add_node(query_curie, "", ())

    query_clique = (query_normalization or {}).get("id") or {}
    query_clique_id = query_clique.get("identifier")
    query_clique_label = query_clique.get("label", "")
    query_clique_keys = {
        curie_key(identifier["identifier"])
        for identifier in (query_normalization or {}).get("equivalent_identifiers", [])
    }
    query_clique_keys.add(curie_key(query_curie))

    depths = _node_depths(query_curie, edges)
    serialized_nodes = []
    for node in nodes.values():
        normalized = normalizations.get(node["id"])
        clique = (normalized or {}).get("id") or {}
        node["clique_id"] = clique.get("identifier")
        node["clique_label"] = clique.get("label", "")
        node["in_query_clique"] = curie_key(node["id"]) in query_clique_keys or (
            query_clique_id is not None and node["clique_id"] == query_clique_id
        )
        if node["in_query_clique"] and node["clique_id"] is None:
            node["clique_id"] = query_clique_id
            node["clique_label"] = query_clique_label
        node["clique_leader"] = bool(
            node["clique_id"] and curie_key(node["id"]) == curie_key(node["clique_id"])
        )
        node["types"] = sorted(node["types"])
        node["depth"] = depths.get(node["id"])
        serialized_nodes.append(node)
    serialized_nodes.sort(
        key=lambda node: (
            node["depth"] is None,
            node["depth"] if node["depth"] is not None else 0,
            node["id"],
        )
    )
    nodes_by_id = {node["id"]: node for node in serialized_nodes}
    for edge in edges:
        subject = nodes_by_id[edge["subject"]]
        obj = nodes_by_id[edge["object"]]
        edge["within_query_clique"] = (
            subject["in_query_clique"] and obj["in_query_clique"]
        )
        edge["cross_clique"] = bool(
            subject["clique_id"]
            and obj["clique_id"]
            and subject["clique_id"] != obj["clique_id"]
        )

    source_counts: dict[str, int] = {}
    source_nodes: dict[str, set[str]] = {}
    for edge in edges:
        source_counts[edge["source"]] = source_counts.get(edge["source"], 0) + 1
        source_nodes.setdefault(edge["source"], set()).update(
            (edge["subject"], edge["object"])
        )

    clique_counts: dict[str | None, dict] = {}
    for node in serialized_nodes:
        clique_id = node["clique_id"]
        clique_record = clique_counts.setdefault(
            clique_id,
            {
                "id": clique_id,
                "label": node["clique_label"],
                "node_count": 0,
                "query": node["in_query_clique"],
            },
        )
        clique_record["node_count"] += 1
        clique_record["query"] = clique_record["query"] or node["in_query_clique"]

    return {
        "query": query_curie,
        "mode": mode,
        "babel_version": babel_version,
        "query_clique": {
            "id": query_clique_id,
            "label": query_clique_label,
            "identifier_count": len(query_clique_keys),
        },
        "nodes": serialized_nodes,
        "edges": edges,
        "sources": [
            {
                "name": source,
                "edge_count": count,
                "node_count": len(source_nodes[source]),
            }
            for source, count in sorted(
                source_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "cliques": sorted(
            clique_counts.values(),
            key=lambda clique: (
                not clique["query"],
                clique["label"] or clique["id"] or "Unresolved",
            ),
        ),
    }


class ViewerService:
    """Application service shared by all HTTP request handlers."""

    def __init__(self, babel_xrefs, name_resolver, babel_version: str | None):
        self.babel_xrefs = babel_xrefs
        self.name_resolver = name_resolver
        self.babel_version = babel_version
        self._graph_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._latest_job_by_query: dict[tuple[str, bool], str] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="babel-viewer"
        )

    def resolve_name(self, query: str) -> list[dict]:
        return [
            dataclasses.asdict(result) for result in self.name_resolver.lookup(query)
        ]

    def get_graph(self, curie: str, recurse: bool = False) -> dict:
        """Build either the starting NodeNorm clique or its full concordance component."""
        nodenorm = self.babel_xrefs.nodenorm
        query_normalization = nodenorm.normalize_curie(curie)
        seed_identifiers = [
            Identifier.from_dict(identifier)
            for identifier in (query_normalization or {}).get(
                "equivalent_identifiers", []
            )
        ]
        if not seed_identifiers:
            seed_identifiers = [Identifier(curie=curie)]

        clique_keys = {curie_key(identifier.curie) for identifier in seed_identifiers}
        lookup_curies = {identifier.curie for identifier in seed_identifiers}
        lookup_curies.update(
            curie_key(identifier.curie) for identifier in seed_identifiers
        )

        # DuckDB queries are intentionally serialized. Running several over the same
        # multi-gigabyte Parquet creates avoidable memory and spill pressure.
        with self._graph_lock:
            xrefs = self.babel_xrefs.get_curie_xrefs(
                [curie] if recurse else sorted(lookup_curies),
                recurse=recurse,
                label_curies=True,
            )
        if not recurse:
            xrefs = [
                xref
                for xref in xrefs
                if curie_key(xref.subj) in clique_keys
                and curie_key(xref.obj) in clique_keys
            ]

        graph_curies = {
            endpoint for xref in xrefs for endpoint in (xref.subj, xref.obj)
        }
        graph_curies.add(curie)
        nodenorm.normalize_curies(graph_curies)
        normalizations = {
            graph_curie: nodenorm.normalize_curie(graph_curie)
            for graph_curie in graph_curies
        }
        return graph_payload(
            curie,
            xrefs,
            self.babel_version,
            mode="recursive" if recurse else "clique",
            query_normalization=query_normalization,
            normalizations=normalizations,
            seed_identifiers=seed_identifiers,
        )

    def start_graph(self, curie: str, recurse: bool = False) -> dict:
        """Queue a graph query and return its job descriptor."""
        with self._jobs_lock:
            query_key = (curie, recurse)
            previous_id = self._latest_job_by_query.get(query_key)
            previous = self._jobs.get(previous_id) if previous_id else None
            if previous and previous["status"] != "failed":
                return self._public_job(previous)

            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "curie": curie,
                "recurse": recurse,
                "status": "queued",
                "created_at": time.monotonic(),
            }
            self._jobs[job_id] = job
            self._latest_job_by_query[query_key] = job_id
            self._prune_jobs()
            self._executor.submit(self._run_graph_job, job_id, curie, recurse)
            return self._public_job(job)

    def get_graph_job(self, job_id: str) -> dict:
        """Return the current public state for one graph query."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return self._public_job(job)

    def _run_graph_job(self, job_id: str, curie: str, recurse: bool) -> None:
        with self._jobs_lock:
            self._jobs[job_id]["status"] = "running"
        try:
            graph = self.get_graph(curie, recurse=recurse)
        except Exception as error:
            # This is the worker boundary. Preserve and log failures instead of letting
            # the executor swallow them.
            logging.exception("Background Babel graph query failed for %s", curie)
            with self._jobs_lock:
                self._jobs[job_id].update(
                    status="failed",
                    error=f"{type(error).__name__}: {error}",
                )
            return
        with self._jobs_lock:
            self._jobs[job_id].update(status="complete", graph=graph)

    @staticmethod
    def _public_job(job: dict) -> dict:
        public = {
            "job_id": job["job_id"],
            "curie": job["curie"],
            "recurse": job["recurse"],
            "status": job["status"],
        }
        if job["status"] == "complete":
            public["graph"] = job["graph"]
        elif job["status"] == "failed":
            public["error"] = job["error"]
        return public

    def _prune_jobs(self) -> None:
        completed = sorted(
            (
                job
                for job in self._jobs.values()
                if job["status"] in {"complete", "failed"}
            ),
            key=lambda job: job["created_at"],
        )
        for job in completed[:-20]:
            self._jobs.pop(job["job_id"], None)
            query_key = (job["curie"], job["recurse"])
            if self._latest_job_by_query.get(query_key) == job["job_id"]:
                self._latest_job_by_query.pop(query_key, None)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def read_static_asset(name: str) -> bytes:
    """Read one packaged viewer asset."""
    return files("babel_explorer").joinpath("web", name).read_bytes()


class ViewerRequestHandler(BaseHTTPRequestHandler):
    """Serve the viewer assets and its two JSON endpoints."""

    service: ViewerService

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in STATIC_ASSETS:
            filename, content_type = STATIC_ASSETS[parsed.path]
            self._send_bytes(
                HTTPStatus.OK,
                read_static_asset(filename),
                content_type,
                cache_control="no-cache",
            )
            return

        params = parse_qs(parsed.query)
        if parsed.path == "/api/resolve":
            self._handle_resolve(params)
            return
        if parsed.path == "/api/graph":
            self._handle_graph(params)
            return
        if parsed.path == "/api/graph-status":
            self._handle_graph_status(params)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_resolve(self, params: dict[str, list[str]]) -> None:
        query = params.get("query", [""])[0].strip()
        if len(query) < 2:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Enter at least two characters to search Name Resolver."},
            )
            return
        if len(query) > 250:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Search text is too long."}
            )
            return
        try:
            results = self.service.resolve_name(query)
        except (requests.RequestException, ValueError) as error:
            logging.exception("Name Resolver lookup failed")
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"Name Resolver lookup failed: {error}"},
            )
            return
        self._send_json(HTTPStatus.OK, {"results": results})

    def _handle_graph(self, params: dict[str, list[str]]) -> None:
        curie = params.get("curie", [""])[0].strip()
        if not curie:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "A CURIE or identifier is required."}
            )
            return
        if len(curie) > 500:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Identifier is too long."}
            )
            return
        recurse_value = params.get("recurse", ["false"])[0].strip().lower()
        if recurse_value not in {"true", "false"}:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "recurse must be either true or false."},
            )
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            self.service.start_graph(curie, recurse=recurse_value == "true"),
        )

    def _handle_graph_status(self, params: dict[str, list[str]]) -> None:
        job_id = params.get("job_id", [""])[0].strip()
        if not job_id:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "A graph job ID is required."}
            )
            return
        try:
            job = self.service.get_graph_job(job_id)
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Graph job not found."})
            return
        self._send_json(HTTPStatus.OK, job)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        self._send_bytes(
            status,
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            cache_control="no-store",
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        cache_control: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger = (
            logging.debug if self.path.startswith("/api/graph-status") else logging.info
        )
        logger("viewer: " + format, *args)


def create_viewer_server(
    service: ViewerService, host: str, port: int
) -> ThreadingHTTPServer:
    """Create a viewer server bound to *host* and *port*."""
    handler = type(
        "ConfiguredViewerRequestHandler",
        (ViewerRequestHandler,),
        {"service": service},
    )
    return ThreadingHTTPServer((host, port), handler)


def serve_viewer(
    service: ViewerService,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
) -> None:
    """Run the viewer until interrupted."""
    server = None
    try:
        server = create_viewer_server(service, host, port)
        browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{browser_host}:{server.server_port}/"
        logging.info("Babel Explorer viewer listening at %s", url)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        service.close()
