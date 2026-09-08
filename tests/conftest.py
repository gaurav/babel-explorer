"""
Shared fixtures for babel-explorer tests.

Session-scoped fixtures download Babel files once and share them across all test modules.
Teardown removes the test data directory so the next run starts fresh.
"""

import os
import shutil

import pytest
import requests
from filelock import FileLock

from babel_explorer.core.babel_xrefs import BabelXRefs
from babel_explorer.core.downloader import BabelDownloader, MissingBabelFileError
from babel_explorer.core.nodenorm import NodeNorm
from tests.constants import (
    BABEL_URL,
    CONCORD_FILE,
    IDENTIFIERS_FILE,
    METADATA_FILE,
    NODENORM_URL,
    TEST_DATA_DIR,
    load_curies,
)

# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def valid_curies() -> list[str]:
    """Load test CURIEs from tests/data/valid_curies.txt."""
    curies = load_curies()
    assert len(curies) > 0, "No CURIEs found in valid_curies.txt"
    return curies


def _selected_any_integration_test(session) -> bool:
    """Did this session actually select a test that uses ``data/test``?"""
    return any(item.get_closest_marker("integration") for item in session.items)


def pytest_sessionfinish(session, exitstatus):
    """Remove the shared test data directory once every worker has finished.

    This runs in the xdist controller, which finishes only after all workers do.
    Workers are identified by having a ``workerinput`` attribute; a plain
    non-parallel run has none either, so cleanup happens there too.

    Cleaning up from a session fixture's teardown instead does not work: with
    ``-n auto`` in ``addopts`` every run is parallel, so each worker would tear
    down at an unpredictable time and gw0 could delete Concord.parquet while gw5
    is still reading it. Guarding that teardown on the worker id, as this used to,
    meant the directory was simply never removed.

    **A session that selected no integration tests cleans up nothing.** ``data/test``
    is a fixed path, not a per-run temporary directory, so an unconditional delete here
    means any ``pytest -m "not integration"`` — the fast loop you run constantly while
    editing — silently destroys the multi-gigabyte download of an integration run going
    on in another terminal. The unit suite never creates or reads that directory, so it
    has no business removing it. Two concurrent *integration* runs still clash; that one
    is unavoidable while the path is shared, and is documented in AGENTS.md.
    """
    if hasattr(session.config, "workerinput"):
        return
    if not _selected_any_integration_test(session):
        return
    if os.path.exists(TEST_DATA_DIR):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def test_data_dir():
    """Provide a test data directory for the entire session.

    Removed by ``pytest_sessionfinish`` once all workers are done, so the next
    run starts fresh.
    """
    os.makedirs(TEST_DATA_DIR, exist_ok=True)
    return TEST_DATA_DIR


def _download_or_skip(downloader, remote_path, test_data_dir, lock_name) -> str:
    """Download one Babel file, skipping the tests that need it if the release omits it.

    One rule, one implementation, for every DuckDB file. A release can publish
    ``Concord.parquet`` without ``Identifiers.parquet`` — ``2026jul22`` does exactly that
    — so "does this release have the Parquet files?" is a question each file has to answer
    for itself, not once for the session. ``MissingBabelFileError`` is what the downloader
    already raises on a 404 for a ``duckdb/`` path, so there is no second HEAD request here
    to drift out of step with the real one.
    """
    lock_path = os.path.join(test_data_dir, lock_name)
    with FileLock(lock_path):
        try:
            return downloader.get_downloaded_file(remote_path)
        except MissingBabelFileError as e:
            pytest.skip(str(e))


@pytest.fixture(scope="session")
def shared_downloader(test_data_dir) -> BabelDownloader:
    """A BabelDownloader pointed at the test data directory.

    Skips the session when the Babel server cannot be reached at all. Whether a given
    file is *published* is settled per file by ``_download_or_skip``, not here — see
    that function for why the two cannot be collapsed into one probe.

    The probe deliberately targets the release root rather than ``Concord.parquet``.
    Naming a file made this look like a publication check, which is what it used to be;
    only the response status told the two apart, and that distinction is gone now.
    Reachability is all this answers, so it asks about the release, not a file in it —
    and the status is not examined, because a 404 from a reachable server is still a
    reachable server.
    """
    try:
        requests.head(BABEL_URL, timeout=30)
    except requests.RequestException as e:
        pytest.skip(f"Babel server unreachable at {BABEL_URL}: {e}")
    return BabelDownloader(url_base=BABEL_URL, local_path=test_data_dir)


@pytest.fixture(scope="session")
def downloaded_concord(shared_downloader, test_data_dir) -> str:
    """Download duckdb/Concord.parquet. Returns the local path.

    Multi-gigabyte in current releases and growing; do not record a figure here,
    it drifts silently and then misleads.
    """
    return _download_or_skip(
        shared_downloader, CONCORD_FILE, test_data_dir, "concord.lock"
    )


@pytest.fixture(scope="session")
def downloaded_metadata(shared_downloader, test_data_dir) -> str:
    """Download duckdb/Metadata.parquet (small). Returns the local path."""
    return _download_or_skip(
        shared_downloader, METADATA_FILE, test_data_dir, "metadata.lock"
    )


@pytest.fixture(scope="session")
def downloaded_parquet_files(downloaded_concord, downloaded_metadata) -> dict[str, str]:
    """Dict of {relative_name: local_path} for Concord and Metadata files."""
    return {
        CONCORD_FILE: downloaded_concord,
        METADATA_FILE: downloaded_metadata,
    }


@pytest.fixture(scope="session")
def downloaded_identifiers(shared_downloader, test_data_dir) -> str:
    """Download duckdb/Identifiers.parquet, the largest file Babel publishes.

    Every test that reaches this is marked ``slow``.

    Skips when the release does not publish it, which is not the same question as
    whether it publishes ``Concord.parquet`` — see ``_download_or_skip``.
    """
    return _download_or_skip(
        shared_downloader, IDENTIFIERS_FILE, test_data_dir, "identifiers.lock"
    )


@pytest.fixture(scope="session")
def nodenorm() -> NodeNorm:
    """A NodeNorm client pointed at the public API.

    Probes ``status`` once and skips the NodeNorm integration tests if it cannot be
    reached, matching what ``shared_downloader`` does for Babel. Without this a RENCI
    outage turned every one of these tests red on CI's push and weekly-cron runs — a
    failure that says nothing about the code under test. The probe cannot cover an
    outage that begins mid-run; it covers the case that actually recurs.
    """
    probe_url = NODENORM_URL.rstrip("/") + "/status"
    try:
        requests.get(probe_url, timeout=30).raise_for_status()
    except requests.RequestException as e:
        pytest.skip(f"NodeNorm unreachable at {probe_url}: {e}")
    return NodeNorm(nodenorm_url=NODENORM_URL)


@pytest.fixture(scope="session")
def babel_xrefs(shared_downloader, downloaded_parquet_files) -> BabelXRefs:
    """A BabelXRefs instance (no NodeNorm) with Concord + Metadata already downloaded."""
    return BabelXRefs(shared_downloader)


@pytest.fixture(scope="session")
def babel_xrefs_with_nodenorm(
    shared_downloader, nodenorm, downloaded_parquet_files
) -> BabelXRefs:
    """A BabelXRefs instance with NodeNorm, Concord + Metadata already downloaded."""
    return BabelXRefs(shared_downloader, nodenorm)
