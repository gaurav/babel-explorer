"""
Tests for the BabelDownloader class.

Unit tests use mocks and run without network access.
Integration tests download real files from the Babel server.
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests
from _pytest.outcomes import Skipped

from babel_explorer.core.downloader import (
    VERSION_MARKER,
    BabelDownloader,
    IncompleteDownloadError,
    MissingBabelFileError,
    compose_babel_url,
    resolve_babel_version,
)
from tests import conftest
from tests.constants import BABEL_URL, CONCORD_FILE


def test_babel_url_is_normalised_for_direct_path_joins():
    """The env-driven URL must end in "/" too, or the skip probe requests
    ".../latestduckdb/Concord.parquet", 404s, and silently skips every integration test."""
    assert BABEL_URL.endswith("/")


class TestComposeBabelUrl:
    """Composition has to absorb the slashes users will and will not type."""

    @pytest.mark.parametrize(
        "releases, version, expected",
        [
            ("https://ex.com/babel/", "latest", "https://ex.com/babel/latest/"),
            ("https://ex.com/babel", "latest", "https://ex.com/babel/latest/"),
            ("https://ex.com/babel//", "2025dec11", "https://ex.com/babel/2025dec11/"),
            ("https://ex.com/babel", "/2025dec11/", "https://ex.com/babel/2025dec11/"),
            ("  https://ex.com/babel  ", "latest", "https://ex.com/babel/latest/"),
            (
                "https://ex.com/babel",
                "  2025dec11  ",
                "https://ex.com/babel/2025dec11/",
            ),
        ],
    )
    def test_normalisation(self, releases, version, expected):
        assert compose_babel_url(releases, version) == expected

    def test_public_default_composes_to_the_historical_url(self):
        """The default pair must reproduce the single URL this option pair replaced."""
        assert (
            compose_babel_url("https://stars.renci.org/var/babel/", "latest")
            == "https://stars.renci.org/var/babel/latest/"
        )


def _version_response(text):
    """A mock requests response serving *text* as the body of VERSION.txt."""
    response = Mock()
    response.text = text
    response.raise_for_status = Mock()
    return response


class TestResolveBabelVersion:
    """Unit tests for resolve_babel_version()."""

    def test_reads_version_txt(self):
        with patch(
            "babel_explorer.core.downloader.requests.get",
            return_value=_version_response(
                "Babel 2026jul22\nhttps://github.com/NCATSTranslator/Babel\n"
            ),
        ) as mock_get:
            assert (
                resolve_babel_version("https://example.com/babel/latest/")
                == "2026jul22"
            )
        assert (
            mock_get.call_args[0][0] == "https://example.com/babel/latest/VERSION.txt"
        )

    def test_version_txt_wins_over_path_segment(self):
        """VERSION.txt is authoritative even when the URL names a version."""
        with patch(
            "babel_explorer.core.downloader.requests.get",
            return_value=_version_response("Babel 2026jul22\n"),
        ):
            assert (
                resolve_babel_version("https://example.com/babel/2025nov19/")
                == "2026jul22"
            )

    def test_falls_back_to_path_segment(self):
        """Trees predating VERSION.txt fall back to the final path segment."""
        with patch(
            "babel_explorer.core.downloader.requests.get",
            side_effect=requests.HTTPError("404"),
        ):
            assert (
                resolve_babel_version("https://example.com/babel/2025nov19/")
                == "2025nov19"
            )

    def test_unresolvable_latest_returns_none(self):
        """'latest' is not a version, so an unreachable VERSION.txt means unknown."""
        with patch(
            "babel_explorer.core.downloader.requests.get",
            side_effect=requests.ConnectionError("boom"),
        ):
            assert resolve_babel_version("https://example.com/babel/latest/") is None

    def test_unparseable_version_txt_falls_back(self):
        with patch(
            "babel_explorer.core.downloader.requests.get",
            return_value=_version_response("something else entirely"),
        ):
            assert (
                resolve_babel_version("https://example.com/babel/2025nov19/")
                == "2025nov19"
            )


class TestSyncCacheVersion:
    """Unit tests for BabelDownloader.sync_cache_version()."""

    @staticmethod
    def _downloader(tmp_path, version):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        dl.babel_version = version
        return dl

    @staticmethod
    def _seed_cache(tmp_path):
        """Create a cached parquet file with its .meta sidecar."""
        duckdb_dir = tmp_path / "duckdb"
        duckdb_dir.mkdir()
        parquet = duckdb_dir / "Concord.parquet"
        parquet.write_text("data")
        meta = duckdb_dir / "Concord.parquet.meta"
        meta.write_text(
            json.dumps({"etag": '"abc"', "last_checked": "2026-07-22T00:00:00+00:00"})
        )
        return parquet, meta

    def test_writes_marker_when_absent(self, tmp_path):
        self._downloader(tmp_path, "2026jul22").sync_cache_version()
        assert (tmp_path / VERSION_MARKER).read_text().strip() == "2026jul22"

    def test_matching_version_keeps_meta(self, tmp_path):
        _, meta = self._seed_cache(tmp_path)
        (tmp_path / VERSION_MARKER).write_text("2026jul22\n")

        self._downloader(tmp_path, "2026jul22").sync_cache_version()

        assert meta.exists()
        assert "last_checked" in json.loads(meta.read_text())

    def test_changed_version_expires_meta_but_keeps_etag_and_parquet(self, tmp_path):
        """The ETag must survive so the refresh costs a HEAD, not a full re-download."""
        parquet, meta = self._seed_cache(tmp_path)
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        self._downloader(tmp_path, "2026jul22").sync_cache_version()

        remaining = json.loads(meta.read_text())
        assert "last_checked" not in remaining, "sidecar should no longer look fresh"
        assert remaining["etag"] == '"abc"', (
            "dropping the ETag would force an unconditional multi-gigabyte re-download"
        )
        assert parquet.exists(), "the Parquet file itself must never be deleted"

    def test_changed_version_leaves_marker_until_the_cache_catches_up(self, tmp_path):
        """A marker written up front makes an interrupted refresh look complete.

        If the marker named the new release straight away and the run died after
        Concord was refreshed but before Identifiers was, the next run would see a
        matching marker, skip the version-driven refresh entirely, and read the two
        Parquet files together across two Babel releases.
        """
        self._seed_cache(tmp_path)
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        self._downloader(tmp_path, "2026jul22").sync_cache_version()

        assert (tmp_path / VERSION_MARKER).read_text().strip() == "2025nov19"

    def test_marker_written_once_every_sidecar_is_revalidated(self, tmp_path):
        _, meta = self._seed_cache(tmp_path)
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        dl = self._downloader(tmp_path, "2026jul22")
        dl.sync_cache_version()

        # What a confirmed-unchanged HEAD or a completed download leaves behind.
        dl._write_meta(str(meta).removesuffix(".meta"), json.loads(meta.read_text()))
        dl._write_version_marker_if_synced()

        assert (tmp_path / VERSION_MARKER).read_text().strip() == "2026jul22"

    def test_marker_withheld_while_one_cached_file_is_still_stale(self, tmp_path):
        """Every cached file must be re-validated, not just the one that was asked for."""
        _, meta = self._seed_cache(tmp_path)
        other = tmp_path / "duckdb" / "Identifiers.parquet.meta"
        other.write_text(
            json.dumps({"etag": '"def"', "last_checked": "2026-07-22T00:00:00+00:00"})
        )
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        dl = self._downloader(tmp_path, "2026jul22")
        dl.sync_cache_version()

        dl._write_meta(str(meta).removesuffix(".meta"), json.loads(meta.read_text()))
        dl._write_version_marker_if_synced()

        assert (tmp_path / VERSION_MARKER).read_text().strip() == "2025nov19"

    def test_changed_version_removes_partial_downloads(self, tmp_path):
        """A .tmp from the previous release must not be resumed against the new one."""
        self._seed_cache(tmp_path)
        partial = tmp_path / "duckdb" / "Concord.parquet.tmp"
        partial.write_text("half of the previous release")
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        self._downloader(tmp_path, "2026jul22").sync_cache_version()

        assert not partial.exists()

    def test_changed_version_drops_unreadable_meta(self, tmp_path):
        _, meta = self._seed_cache(tmp_path)
        meta.write_text("not json")
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        self._downloader(tmp_path, "2026jul22").sync_cache_version()

        assert not meta.exists()

    def test_refresh_does_not_reach_into_sibling_directories(self, tmp_path):
        """local_path may hold other Babel releases; only our own duckdb/ is cleared."""
        self._seed_cache(tmp_path)
        sibling = tmp_path / "2025nov19" / "duckdb"
        sibling.mkdir(parents=True)
        sibling_meta = sibling / "Concord.parquet.meta"
        sibling_meta.write_text("{}")
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        self._downloader(tmp_path, "2026jul22").sync_cache_version()

        assert sibling_meta.exists(), (
            "a nested release directory must not be swept up in the refresh"
        )

    def test_unknown_version_leaves_cache_untouched(self, tmp_path):
        """An unresolvable version must not trigger a multi-gigabyte re-download."""
        _, meta = self._seed_cache(tmp_path)
        before = meta.read_text()
        (tmp_path / VERSION_MARKER).write_text("2025nov19\n")

        self._downloader(tmp_path, None).sync_cache_version()

        assert meta.read_text() == before
        assert (tmp_path / VERSION_MARKER).read_text().strip() == "2025nov19"


class TestMissingBabelFile:
    """A 404 should explain itself and not be retried."""

    def test_404_raises_immediately(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=10
        )
        dl.babel_version = "2025dec11"

        response = MagicMock()
        response.status_code = 404
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch(
            "babel_explorer.core.downloader.requests.get", return_value=response
        ) as mock_get:
            with pytest.raises(MissingBabelFileError, match="2025dec11"):
                dl.get_downloaded_file(CONCORD_FILE)

        assert mock_get.call_count == 1, "a 404 must not be retried"

    def test_404_message_names_the_current_setting(self, tmp_path):
        """This message is where most people learn the config scheme exists.

        It named BABEL_URL for as long as that variable did; nothing caught the
        wording when the variable was replaced. Pin it to the setting that works.
        """
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        # Preset so the patched requests.get is not also asked to resolve VERSION.txt.
        dl.babel_version = "2025dec11"
        response = MagicMock(status_code=404)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch(
            "babel_explorer.core.downloader.requests.get", return_value=response
        ):
            with pytest.raises(MissingBabelFileError) as excinfo:
                dl.get_downloaded_file(CONCORD_FILE)

        message = str(excinfo.value)
        assert "BABEL_RELEASES_URL" in message
        assert "--babel-url" in message
        assert "set BABEL_URL" not in message


# ==========================================================================
# Unit Tests — no network required
# ==========================================================================


class TestBabelDownloaderInit:
    """Tests for BabelDownloader constructor."""

    def test_constructor_stores_url_and_path(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        assert dl.url_base == "https://example.com/"
        assert dl.local_path == str(tmp_path)

    def test_creates_directory_if_missing(self, tmp_path):
        new_dir = str(tmp_path / "nested" / "dir")
        dl = BabelDownloader(url_base="https://example.com/", local_path=new_dir)
        assert os.path.isdir(new_dir)
        assert dl.local_path == new_dir

    def test_custom_retries(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        assert dl.retries == 3

    def test_default_retries(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        assert dl.retries == 10

    def test_default_freshness_seconds(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        assert dl.freshness_seconds == 3 * 3600

    def test_custom_freshness_seconds(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/",
            local_path=str(tmp_path),
            freshness_seconds=0,
        )
        assert dl.freshness_seconds == 0

    def test_url_base_trailing_slash_added(self, tmp_path):
        """url_base without trailing slash gets one appended automatically."""
        dl = BabelDownloader(
            url_base="https://example.com/path", local_path=str(tmp_path)
        )
        assert dl.url_base == "https://example.com/path/"

    def test_url_base_with_trailing_slash_unchanged(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/path/", local_path=str(tmp_path)
        )
        assert dl.url_base == "https://example.com/path/"

    def test_invalid_path_raises_value_error(self):
        """Using a file path (not a directory) should raise ValueError."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"not a directory")
            f.flush()
            try:
                with pytest.raises(ValueError, match="Invalid local_path"):
                    BabelDownloader(url_base="https://example.com/", local_path=f.name)
            finally:
                os.unlink(f.name)


class TestSaveMeta:
    """Tests for _save_meta."""

    def _make_dl(self, tmp_path):
        return BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path)
        )

    def test_writes_all_fields(self, tmp_path):
        dl = self._make_dl(tmp_path)
        file_path = str(tmp_path / "test.parquet")
        # Create the file so the path is valid
        open(file_path, "wb").close()

        headers = {
            "ETag": '"abc123"',
            "Last-Modified": "Wed, 03 Dec 2025 15:54:19 GMT",
            "Content-Length": "12345",
        }
        dl._save_meta(file_path, headers)

        meta_path = file_path + ".meta"
        assert os.path.exists(meta_path)
        with open(meta_path) as f:
            meta = json.load(f)

        assert meta["etag"] == '"abc123"'
        assert meta["last_modified"] == "Wed, 03 Dec 2025 15:54:19 GMT"
        assert meta["content_length"] == 12345
        assert "last_checked" in meta

    def test_last_checked_is_recent_utc(self, tmp_path):
        dl = self._make_dl(tmp_path)
        file_path = str(tmp_path / "f.parquet")
        open(file_path, "wb").close()

        dl._save_meta(file_path, {"ETag": '"x"'})

        with open(file_path + ".meta") as f:
            meta = json.load(f)

        last_checked = datetime.fromisoformat(meta["last_checked"])
        age = (datetime.now(UTC) - last_checked).total_seconds()
        assert age < 5  # written less than 5 seconds ago

    def test_missing_headers_not_written(self, tmp_path):
        """Headers not present in the response should not appear in .meta."""
        dl = self._make_dl(tmp_path)
        file_path = str(tmp_path / "sparse.parquet")
        open(file_path, "wb").close()

        dl._save_meta(file_path, {})

        with open(file_path + ".meta") as f:
            meta = json.load(f)

        assert "etag" not in meta
        assert "last_modified" not in meta
        assert "content_length" not in meta
        assert "last_checked" in meta


class TestLoadMeta:
    """Tests for _load_meta."""

    def _make_dl(self, tmp_path):
        return BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path)
        )

    def test_returns_none_if_no_meta_file(self, tmp_path):
        dl = self._make_dl(tmp_path)
        assert dl._load_meta(str(tmp_path / "nonexistent.parquet")) is None

    def test_returns_dict_for_valid_meta(self, tmp_path):
        dl = self._make_dl(tmp_path)
        file_path = str(tmp_path / "f.parquet")
        open(file_path, "wb").close()
        meta_data = {"etag": '"abc"', "last_checked": "2026-01-01T00:00:00+00:00"}
        with open(file_path + ".meta", "w") as f:
            json.dump(meta_data, f)

        result = dl._load_meta(file_path)
        assert result == meta_data

    def test_returns_none_for_corrupt_meta(self, tmp_path):
        dl = self._make_dl(tmp_path)
        file_path = str(tmp_path / "corrupt.parquet")
        open(file_path, "wb").close()
        with open(file_path + ".meta", "w") as f:
            f.write("not valid json {{{")

        assert dl._load_meta(file_path) is None


class TestIsWithinFreshness:
    """Tests for _is_within_freshness."""

    def _make_dl(self, tmp_path):
        return BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path)
        )

    def test_returns_true_when_recent(self, tmp_path):
        dl = self._make_dl(tmp_path)
        recent = datetime.now(UTC).isoformat()
        meta = {"last_checked": recent}
        assert dl._is_within_freshness(meta, 3600) is True

    def test_returns_false_when_stale(self, tmp_path):
        dl = self._make_dl(tmp_path)
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        meta = {"last_checked": old}
        assert dl._is_within_freshness(meta, 3600) is False

    def test_returns_false_when_missing_last_checked(self, tmp_path):
        dl = self._make_dl(tmp_path)
        assert dl._is_within_freshness({}, 3600) is False

    def test_returns_true_when_freshness_is_inf(self, tmp_path):
        dl = self._make_dl(tmp_path)
        old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        meta = {"last_checked": old}
        assert dl._is_within_freshness(meta, float("inf")) is True

    def test_returns_false_when_missing_last_checked_even_if_inf(self, tmp_path):
        """`--check-download never` must not resurrect a sidecar the version change expired.

        sync_cache_version clears last_checked to force a re-check. If float('inf')
        short-circuited ahead of that test, `never` would return the previous release's
        Parquet with no network call at all.
        """
        dl = self._make_dl(tmp_path)
        assert dl._is_within_freshness({"etag": '"old"'}, float("inf")) is False

    def test_returns_false_when_freshness_is_zero(self, tmp_path):
        dl = self._make_dl(tmp_path)
        just_now = datetime.now(UTC).isoformat()
        meta = {"last_checked": just_now}
        # Even with freshness=0, age >= 0 so it's not < 0
        assert dl._is_within_freshness(meta, 0) is False


class TestRemoteUnchanged:
    """Tests for _remote_unchanged."""

    def _make_dl(self, tmp_path):
        return BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path)
        )

    def test_returns_true_on_matching_etag(self, tmp_path):
        dl = self._make_dl(tmp_path)
        meta = {"etag": '"abc123"'}
        mock_resp = Mock()
        mock_resp.headers = {"ETag": '"abc123"'}
        mock_resp.raise_for_status = Mock()
        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_resp
        ):
            assert dl._remote_unchanged("https://example.com/f.parquet", meta) is True

    def test_returns_false_on_different_etag(self, tmp_path):
        dl = self._make_dl(tmp_path)
        meta = {"etag": '"old"'}
        mock_resp = Mock()
        mock_resp.headers = {"ETag": '"new"'}
        mock_resp.raise_for_status = Mock()
        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_resp
        ):
            assert dl._remote_unchanged("https://example.com/f.parquet", meta) is False

    def test_fallback_last_modified_match(self, tmp_path):
        dl = self._make_dl(tmp_path)
        lm = "Wed, 03 Dec 2025 15:54:19 GMT"
        meta = {"last_modified": lm, "content_length": 100}
        mock_resp = Mock()
        mock_resp.headers = {"Last-Modified": lm, "Content-Length": "100"}
        mock_resp.raise_for_status = Mock()
        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_resp
        ):
            assert dl._remote_unchanged("https://example.com/f.parquet", meta) is True

    def test_returns_none_on_request_error(self, tmp_path):
        """A failed HEAD is 'unknown', not 'unchanged' — the caller keeps the cached
        file but must not restamp last_checked on the strength of it."""
        dl = self._make_dl(tmp_path)
        meta = {"etag": '"abc"'}
        with patch(
            "babel_explorer.core.downloader.requests.head",
            side_effect=requests.ConnectionError("fail"),
        ):
            assert dl._remote_unchanged("https://example.com/f.parquet", meta) is None


class TestGetDownloadedFileTiers:
    """Tests for the three-tier logic in get_downloaded_file."""

    def _make_dl(self, tmp_path, freshness=3600):
        return BabelDownloader(
            url_base="https://example.com/",
            local_path=str(tmp_path),
            freshness_seconds=freshness,
        )

    # --- Tier 1: within freshness window ---

    def test_tier1_returns_immediately_no_http(self, tmp_path):
        """File + fresh .meta → no network calls at all."""
        dl = self._make_dl(tmp_path, freshness=3600)
        test_file = "duckdb/test.parquet"
        local = tmp_path / "duckdb" / "test.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"data")

        meta = {"etag": '"abc"', "last_checked": datetime.now(UTC).isoformat()}
        with open(str(local) + ".meta", "w") as f:
            json.dump(meta, f)

        with patch("babel_explorer.core.downloader.requests.head") as mock_head:
            with patch("babel_explorer.core.downloader.requests.get") as mock_get:
                result = dl.get_downloaded_file(test_file)
                mock_head.assert_not_called()
                mock_get.assert_not_called()
        assert result == str(local)

    def test_never_still_rechecks_after_a_release_change(self, tmp_path):
        """`--check-download never` must not defeat the cross-release refresh.

        End-to-end version of the _is_within_freshness ordering: the cache holds the
        previous release, sync_cache_version has expired the sidecar, and `never` still
        has to issue the HEAD that notices the ETag changed.
        """
        dl = self._make_dl(tmp_path, freshness=float("inf"))
        test_file = "duckdb/test.parquet"
        local = tmp_path / "duckdb" / "test.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"data from the previous release")

        # No last_checked: exactly what sync_cache_version leaves behind.
        with open(str(local) + ".meta", "w") as f:
            json.dump({"etag": '"old"'}, f)

        mock_head_resp = Mock()
        mock_head_resp.headers = {"ETag": '"new"'}
        mock_head_resp.raise_for_status = Mock()

        def fake_download(url, tmp_path_, chunk_size):
            """Stand in for the real download by writing the .tmp it would have left."""
            with open(tmp_path_, "wb") as f:
                f.write(b"the new release")
            return {"ETag": '"new"'}

        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_head_resp
        ) as mock_head:
            with patch.object(
                dl, "_download_with_retry", side_effect=fake_download
            ) as mock_download:
                dl.get_downloaded_file(test_file)

        mock_head.assert_called_once()
        mock_download.assert_called_once()
        assert local.read_bytes() == b"the new release"

    # --- Tier 2: stale .meta, ETag matches ---

    def test_tier2_head_check_no_redownload(self, tmp_path):
        """Stale .meta + matching ETag → HEAD only, no GET."""
        dl = self._make_dl(tmp_path, freshness=0)
        test_file = "duckdb/test.parquet"
        local = tmp_path / "duckdb" / "test.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"data")

        old_ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        meta = {"etag": '"abc"', "last_checked": old_ts}
        with open(str(local) + ".meta", "w") as f:
            json.dump(meta, f)

        mock_head_resp = Mock()
        mock_head_resp.headers = {"ETag": '"abc"'}
        mock_head_resp.raise_for_status = Mock()

        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_head_resp
        ):
            with patch("babel_explorer.core.downloader.requests.get") as mock_get:
                result = dl.get_downloaded_file(test_file)
                mock_get.assert_not_called()
        assert result == str(local)

    def test_tier2_updates_last_checked_after_head(self, tmp_path):
        """After successful HEAD match, last_checked in .meta is updated."""
        dl = self._make_dl(tmp_path, freshness=0)
        test_file = "duckdb/upd.parquet"
        local = tmp_path / "duckdb" / "upd.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"data")

        old_ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        meta = {"etag": '"abc"', "last_checked": old_ts}
        with open(str(local) + ".meta", "w") as f:
            json.dump(meta, f)

        mock_head_resp = Mock()
        mock_head_resp.headers = {"ETag": '"abc"'}
        mock_head_resp.raise_for_status = Mock()

        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_head_resp
        ):
            dl.get_downloaded_file(test_file)

        with open(str(local) + ".meta") as f:
            updated_meta = json.load(f)
        updated_ts = datetime.fromisoformat(updated_meta["last_checked"])
        assert (datetime.now(UTC) - updated_ts).total_seconds() < 5

    def test_tier2_failed_head_does_not_refresh_last_checked(self, tmp_path):
        """A HEAD that never happened must not mark the file freshly validated.

        sync_cache_version clears last_checked when the Babel release changes so
        every cached file is re-checked. If one flaky HEAD restamped it, the old
        release's Parquet would look current for the whole freshness window under a
        .babel-version marker naming the new release.
        """
        dl = self._make_dl(tmp_path, freshness=3600)
        test_file = "duckdb/unreachable.parquet"
        local = tmp_path / "duckdb" / "unreachable.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"data from the previous release")

        # No last_checked: exactly what sync_cache_version leaves behind.
        with open(str(local) + ".meta", "w") as f:
            json.dump({"etag": '"old"'}, f)

        with patch(
            "babel_explorer.core.downloader.requests.head",
            side_effect=requests.ConnectionError("network down"),
        ):
            with patch("babel_explorer.core.downloader.requests.get") as mock_get:
                result = dl.get_downloaded_file(test_file)
                mock_get.assert_not_called()

        assert result == str(local)
        with open(str(local) + ".meta") as f:
            assert "last_checked" not in json.load(f)

    # --- Tier 3: ETag changed, re-download ---

    def test_tier3_redownloads_when_etag_changed(self, tmp_path):
        """Changed ETag → file deleted and re-downloaded."""
        dl = self._make_dl(tmp_path, freshness=0)
        test_file = "duckdb/changed.parquet"
        local = tmp_path / "duckdb" / "changed.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"old data")

        old_ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        meta = {"etag": '"old"', "last_checked": old_ts}
        with open(str(local) + ".meta", "w") as f:
            json.dump(meta, f)

        mock_head_resp = Mock()
        mock_head_resp.headers = {"ETag": '"new"'}
        mock_head_resp.raise_for_status = Mock()

        new_content = b"new data"

        def fake_download(url, path, chunk_size):
            with open(path, "wb") as f:
                f.write(new_content)
            return {"ETag": '"new"', "Content-Length": str(len(new_content))}

        with patch(
            "babel_explorer.core.downloader.requests.head", return_value=mock_head_resp
        ):
            with patch.object(dl, "_download_with_retry", side_effect=fake_download):
                result = dl.get_downloaded_file(test_file)

        assert open(result, "rb").read() == new_content

    # --- No .meta: fresh download ---

    def test_downloads_when_no_meta(self, tmp_path):
        """No file and no .meta → download happens, .meta is saved."""
        dl = self._make_dl(tmp_path)
        test_file = "duckdb/new.parquet"
        content = b"fresh download"

        def fake_download(url, path, chunk_size):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
            return {"ETag": '"fresh"', "Content-Length": str(len(content))}

        with patch.object(
            dl, "_download_with_retry", side_effect=fake_download
        ) as mock_dl:
            result = dl.get_downloaded_file(test_file)
            mock_dl.assert_called_once()

        assert os.path.exists(result)
        assert open(result, "rb").read() == content
        # .meta should be saved
        meta_path = result + ".meta"
        assert os.path.exists(meta_path)
        with open(meta_path) as f:
            saved_meta = json.load(f)
        assert saved_meta["etag"] == '"fresh"'

    def test_downloads_when_file_exists_but_no_meta(self, tmp_path):
        """File exists but no .meta → treats as unknown, triggers full download flow."""
        dl = self._make_dl(tmp_path, freshness=3600)
        test_file = "duckdb/nometa.parquet"
        local = tmp_path / "duckdb" / "nometa.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"old content")
        # No .meta file

        new_content = b"refreshed"

        def fake_download(url, path, chunk_size):
            with open(path, "wb") as f:
                f.write(new_content)
            return {"ETag": '"new"'}

        with patch.object(
            dl, "_download_with_retry", side_effect=fake_download
        ) as mock_dl:
            result = dl.get_downloaded_file(test_file)
            mock_dl.assert_called_once()

        assert open(result, "rb").read() == new_content


class TestPartialDownloadSafety:
    """A .tmp must never be resumed across two different versions of a remote file."""

    def test_leftover_tmp_from_an_earlier_run_is_discarded(self, tmp_path):
        """A .tmp of unknown provenance is deleted before the download starts.

        get_downloaded_file only reaches the download block when the remote bytes
        changed, so resuming an orphaned .tmp (left by a killed process) would
        append the new file's tail to the old file's prefix and then stamp the
        splice with the new ETag.
        """
        dl = BabelDownloader(
            url_base="https://example.com/",
            local_path=str(tmp_path),
            freshness_seconds=0,
        )
        test_file = "duckdb/spliced.parquet"
        local = tmp_path / "duckdb" / "spliced.parquet"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"old version")
        with open(str(local) + ".meta", "w") as f:
            json.dump({"etag": '"old"'}, f)
        tmp_file = local.parent / "spliced.parquet.tmp"
        tmp_file.write_bytes(b"PREFIX-OF-OLD-VERSION")

        seen_sizes = []

        def fake_download(url, path, chunk_size):
            seen_sizes.append(os.path.getsize(path) if os.path.exists(path) else None)
            with open(path, "wb") as f:
                f.write(b"new version")
            return {"ETag": '"new"'}

        head = Mock(headers={"ETag": '"new"'}, raise_for_status=Mock())
        with (
            patch("babel_explorer.core.downloader.requests.head", return_value=head),
            patch.object(dl, "_download_with_retry", side_effect=fake_download),
        ):
            result = dl.get_downloaded_file(test_file)

        assert seen_sizes == [None], "the stale .tmp was still on disk"
        assert open(result, "rb").read() == b"new version"

    def test_keyboard_interrupt_removes_the_partial_file(self, tmp_path):
        """Ctrl-C is a BaseException; the .tmp must still be cleaned up."""
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        tmp_file = tmp_path / "interrupted.parquet.tmp"

        def fake_download(url, path, chunk_size):
            with open(path, "wb") as f:
                f.write(b"half a file")
            raise KeyboardInterrupt

        with patch.object(dl, "_download_with_retry", side_effect=fake_download):
            with pytest.raises(KeyboardInterrupt):
                dl.get_downloaded_file("interrupted.parquet")

        assert not tmp_file.exists()

    def test_resume_sends_if_range_once_a_validator_is_known(self, tmp_path):
        """After the first response, a resume is conditional on the file not changing."""
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        out_path = str(tmp_path / "conditional.bin")

        # First attempt streams 4 of 10 bytes and then trips the size check.
        first = TestDownloadWithRetry._make_response(
            200, {"Content-Length": "10", "ETag": '"v1"'}, [b"abcd"]
        )
        second = TestDownloadWithRetry._make_response(
            206, {"Content-Length": "6", "ETag": '"v1"'}, [b"efghij"]
        )
        with (
            patch(
                "babel_explorer.core.downloader.requests.get",
                side_effect=[first, second],
            ) as mock_get,
            patch("babel_explorer.core.downloader.time.sleep"),
        ):
            dl._download_with_retry("https://example.com/file", out_path, 1024)

        assert mock_get.call_args_list[1].kwargs["headers"] == {
            "Range": "bytes=4-",
            "If-Range": '"v1"',
        }
        assert open(out_path, "rb").read() == b"abcdefghij"


class TestDownloadCompleteness:
    """A short stream must be retried, never promoted as the finished file."""

    def test_truncated_stream_raises(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = str(tmp_path / "short.bin")

        mock_response = Mock()
        mock_response.headers = {"Content-Length": "10"}
        mock_response.iter_content = Mock(return_value=[b"only4"])

        with pytest.raises(IncompleteDownloadError, match="expected 10 bytes"):
            dl._stream_download(mock_response, out_path, 0, 1024)

    def test_truncated_stream_is_retried_and_resumed(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        out_path = str(tmp_path / "resumed.bin")

        # The ETag is what makes the resume conditional, and so allowed at all:
        # without a validator the retry restarts from zero instead.
        first = TestDownloadWithRetry._make_response(
            200, {"Content-Length": "10", "ETag": '"v1"'}, [b"abcd"]
        )
        second = TestDownloadWithRetry._make_response(
            206, {"Content-Length": "6", "ETag": '"v1"'}, [b"efghij"]
        )
        with (
            patch(
                "babel_explorer.core.downloader.requests.get",
                side_effect=[first, second],
            ),
            patch("babel_explorer.core.downloader.time.sleep"),
        ):
            dl._download_with_retry("https://example.com/file", out_path, 1024)

        assert open(out_path, "rb").read() == b"abcdefghij"

    def test_encoded_body_skips_the_size_check(self, tmp_path):
        """With Content-Encoding set, iter_content yields decoded bytes whose count
        has nothing to do with Content-Length."""
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = str(tmp_path / "gzipped.bin")

        mock_response = Mock()
        mock_response.headers = {"Content-Length": "4", "Content-Encoding": "gzip"}
        mock_response.iter_content = Mock(return_value=[b"decompressed"])

        dl._stream_download(mock_response, out_path, 0, 1024)
        assert open(out_path, "rb").read() == b"decompressed"

    def test_416_with_a_shorter_remote_file_restarts(self, tmp_path):
        """A remote rebuild that shrank the file also answers 416; the over-long
        local file must not be promoted as complete."""
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        out_path = tmp_path / "shrunk.bin"

        # Attempt 1 ends short, leaving 20 bytes and a validator behind. Only then is
        # there a Range to send, and so a 416 to receive.
        truncated = TestDownloadWithRetry._make_response(
            200, {"Content-Length": "30", "ETag": '"old"'}, [b"the old, longer file"]
        )
        too_long = TestDownloadWithRetry._make_response(416)
        fresh = TestDownloadWithRetry._make_response(
            200, {"Content-Length": "5", "ETag": '"new"'}, [b"short"]
        )
        head = MagicMock(status_code=200, headers={"Content-Length": "5"})
        with (
            patch(
                "babel_explorer.core.downloader.requests.get",
                side_effect=[truncated, too_long, fresh],
            ),
            patch("babel_explorer.core.downloader.requests.head", return_value=head),
            patch("babel_explorer.core.downloader.time.sleep"),
        ):
            headers = dl._download_with_retry(
                "https://example.com/file", str(out_path), 1024
            )

        assert out_path.read_bytes() == b"short"
        assert headers["ETag"] == '"new"'


class TestFullContentLength:
    """A 206 Content-Length is a range length, not the file's length."""

    def test_partial_response_records_the_total_from_content_range(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        file_path = str(tmp_path / "resumed.parquet")
        with open(file_path, "wb") as f:
            f.write(b"0123456789")

        dl._save_meta(
            file_path,
            {"Content-Length": "6", "Content-Range": "bytes 4-9/10", "ETag": '"e"'},
        )
        with open(file_path + ".meta") as f:
            assert json.load(f)["content_length"] == 10

    def test_partial_response_with_unknown_total_falls_back_to_the_file(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        file_path = str(tmp_path / "unknown_total.parquet")
        with open(file_path, "wb") as f:
            f.write(b"0123456789")

        dl._save_meta(
            file_path, {"Content-Length": "6", "Content-Range": "bytes 4-9/*"}
        )
        with open(file_path + ".meta") as f:
            assert json.load(f)["content_length"] == 10


class TestGetDownloadedFileCaching:
    """Tests that repeated calls within the freshness window avoid redundant downloads."""

    def test_second_call_within_freshness_skips_download(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        content = b"cached content"

        def fake_download(url, path, chunk_size):
            with open(path, "wb") as f:
                f.write(content)
            return {}

        with patch.object(
            dl, "_download_with_retry", side_effect=fake_download
        ) as mock_dl:
            r1 = dl.get_downloaded_file("cached.txt")
            r2 = dl.get_downloaded_file("cached.txt")
            assert r1 == r2
            mock_dl.assert_called_once()  # freshness window prevents second download


class TestDownloadWithRetry:
    """Tests for _download_with_retry."""

    @staticmethod
    def _make_response(status_code, headers=None, content=None):
        m = MagicMock()
        m.__enter__.return_value = m
        m.status_code = status_code
        m.headers = headers or {}
        if content is not None:
            m.iter_content = Mock(return_value=content)
        return m

    def test_retries_exhausted_raises_runtime_error(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=2
        )
        with patch(
            "babel_explorer.core.downloader.requests.get",
            side_effect=requests.ConnectionError("fail"),
        ):
            with patch("babel_explorer.core.downloader.time.sleep"):  # skip waiting
                with pytest.raises(RuntimeError, match="Failed to download"):
                    dl._download_with_retry(
                        "https://example.com/file", str(tmp_path / "f"), 1024
                    )

    def test_succeeds_on_second_attempt(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        out_path = str(tmp_path / "retry_success.bin")

        mock_response = self._make_response(200, {"Content-Length": "5"}, [b"hello"])
        side_effects = [requests.ConnectionError("first fail"), mock_response]

        with patch(
            "babel_explorer.core.downloader.requests.get", side_effect=side_effects
        ):
            with patch("babel_explorer.core.downloader.time.sleep"):
                dl._download_with_retry("https://example.com/file", out_path, 1024)
        assert os.path.exists(out_path)

    def test_resume_without_a_validator_restarts_from_zero(self, tmp_path):
        """A bare Range is a splice waiting to happen.

        With neither an ETag nor a Last-Modified there is no If-Range to send, so
        nothing stops a server that rebuilt the file from handing back the new
        version's tail to append to the old version's prefix — and the result would
        be stamped with the new validator and pass every later check. Restarting
        costs a re-download; resuming costs silent corruption.
        """
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = tmp_path / "partial.bin"
        out_path.write_bytes(b"partial")  # 7 bytes

        mock_response = self._make_response(200, {"Content-Length": "5"}, [b"whole"])
        with patch(
            "babel_explorer.core.downloader.requests.get", return_value=mock_response
        ) as mock_get:
            dl._download_with_retry("https://example.com/file", str(out_path), 1024)
            _, kwargs = mock_get.call_args
            assert kwargs["headers"] == {}, "no validator means no conditional resume"
        assert out_path.read_bytes() == b"whole"

    def test_http_416_file_already_complete(self, tmp_path):
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        out_path = tmp_path / "complete.bin"

        # Attempt 1 over-declares Content-Length and delivers the whole 9-byte file,
        # so the size check retries it; attempt 2 then asks for bytes past the end of
        # a file it already holds in full, which is what a 416 legitimately means.
        over_declared = self._make_response(
            200, {"Content-Length": "20", "ETag": '"v1"'}, [b"full file"]
        )
        mock_response = self._make_response(416)
        head = MagicMock(status_code=200, headers={"Content-Length": "9"})
        with (
            patch(
                "babel_explorer.core.downloader.requests.get",
                side_effect=[over_declared, mock_response],
            ),
            patch("babel_explorer.core.downloader.requests.head", return_value=head),
            patch("babel_explorer.core.downloader.time.sleep"),
        ):
            headers = dl._download_with_retry(
                "https://example.com/file", str(out_path), 1024
            )
        assert out_path.read_bytes() == b"full file"
        # The 416 response describes the error body; saving its length as the file's
        # metadata would fail every later freshness check and re-download the file.
        assert headers == {"Content-Length": "9"}

    def test_416_without_a_content_length_restarts(self, tmp_path):
        """416 alone does not prove completeness — it is also how a shrunk file answers.

        With no remote length to compare against there is no way to tell the two
        apart, so the local file must not be promoted as complete.
        """
        dl = BabelDownloader(
            url_base="https://example.com/", local_path=str(tmp_path), retries=3
        )
        out_path = tmp_path / "unverifiable.bin"

        # Attempt 1 ends short, leaving bytes and a validator behind, so attempt 2
        # sends the Range that draws the 416.
        truncated = self._make_response(
            200, {"Content-Length": "30", "ETag": '"old"'}, [b"possibly stale bytes"]
        )
        range_rejected = self._make_response(416)
        fresh = self._make_response(
            200, {"Content-Length": "5", "ETag": '"new"'}, [b"fresh"]
        )
        head = MagicMock(status_code=200, headers={})
        with (
            patch(
                "babel_explorer.core.downloader.requests.get",
                side_effect=[truncated, range_rejected, fresh],
            ),
            patch("babel_explorer.core.downloader.requests.head", return_value=head),
            patch("babel_explorer.core.downloader.time.sleep"),
        ):
            headers = dl._download_with_retry(
                "https://example.com/file", str(out_path), 1024
            )

        assert out_path.read_bytes() == b"fresh"
        assert headers["ETag"] == '"new"'

    def test_server_no_resume_restarts_download(self, tmp_path):
        """When server responds 200 (instead of 206), partial file is removed and download restarts."""
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = tmp_path / "no_resume.bin"
        out_path.write_bytes(b"partial")

        mock_response = self._make_response(
            200, {"Content-Length": "12"}, [b"full content"]
        )
        with patch(
            "babel_explorer.core.downloader.requests.get", return_value=mock_response
        ):
            dl._download_with_retry("https://example.com/file", str(out_path), 1024)
        assert out_path.read_bytes() == b"full content"

    def test_returns_response_headers(self, tmp_path):
        """_download_with_retry should return response headers."""
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = str(tmp_path / "headers.bin")

        mock_response = self._make_response(
            200, {"Content-Length": "5", "ETag": '"abc"'}, [b"hello"]
        )
        with patch(
            "babel_explorer.core.downloader.requests.get", return_value=mock_response
        ):
            headers = dl._download_with_retry(
                "https://example.com/file", out_path, 1024
            )
        assert headers["ETag"] == '"abc"'


class TestStreamDownload:
    """Tests for _stream_download."""

    def test_writes_chunks(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = str(tmp_path / "stream.bin")

        mock_response = Mock()
        mock_response.headers = {"Content-Length": "10"}
        mock_response.iter_content = Mock(return_value=[b"hello", b"world"])

        dl._stream_download(mock_response, out_path, resume_byte_pos=0, chunk_size=1024)
        with open(out_path, "rb") as f:
            assert f.read() == b"helloworld"

    def test_append_mode_on_resume(self, tmp_path):
        dl = BabelDownloader(url_base="https://example.com/", local_path=str(tmp_path))
        out_path = tmp_path / "append.bin"
        out_path.write_bytes(b"start")

        mock_response = Mock()
        mock_response.headers = {"Content-Length": "3"}
        mock_response.iter_content = Mock(return_value=[b"end"])

        dl._stream_download(
            mock_response, str(out_path), resume_byte_pos=5, chunk_size=1024
        )
        assert out_path.read_bytes() == b"startend"


# ==========================================================================
# Integration Tests — require network access
# ==========================================================================


@pytest.mark.integration
def test_download_concord_parquet(downloaded_concord):
    """Verify Concord.parquet downloads and is > 100 MB."""
    assert os.path.isfile(downloaded_concord)
    size = os.path.getsize(downloaded_concord)
    assert size > 100 * 1024 * 1024, f"Concord.parquet too small: {size} bytes"


@pytest.mark.integration
def test_download_metadata_parquet(downloaded_metadata):
    """Verify Metadata.parquet downloads and is non-empty."""
    assert os.path.isfile(downloaded_metadata)
    assert os.path.getsize(downloaded_metadata) > 0


@pytest.mark.integration
def test_download_creates_meta_file(downloaded_concord):
    """After download, a .meta sidecar file should exist."""
    meta_path = downloaded_concord + ".meta"
    assert os.path.isfile(meta_path), f"Missing .meta file: {meta_path}"
    with open(meta_path) as f:
        meta = json.load(f)
    assert "last_checked" in meta


@pytest.mark.integration
def test_download_caching_real_files(shared_downloader, downloaded_concord):
    """Second call returns same path and file is not re-downloaded."""
    path2 = shared_downloader.get_downloaded_file(CONCORD_FILE)
    assert path2 == downloaded_concord
    assert os.path.getmtime(downloaded_concord) == os.path.getmtime(path2)


@pytest.mark.integration
@pytest.mark.slow
def test_download_identifiers_parquet(downloaded_identifiers):
    """Verify Identifiers.parquet downloads as a complete Parquet file.

    Checks the format rather than a byte count. A hard size figure is exactly the kind
    of number AGENTS.md says drifts silently and then misleads — it was ``> 2 GB``,
    chosen against a release long superseded — and the ``PAR1`` marker at both ends is
    a better test of the thing that actually goes wrong: a truncated download, or an
    error page saved under a .parquet name.
    """
    assert os.path.isfile(downloaded_identifiers)
    assert os.path.getsize(downloaded_identifiers) > 8, "too short to be a Parquet file"
    with open(downloaded_identifiers, "rb") as f:
        assert f.read(4) == b"PAR1", "missing Parquet header"
        f.seek(-4, os.SEEK_END)
        assert f.read(4) == b"PAR1", "missing Parquet footer — download was truncated"


class TestIdentifiersFixtureSkips:
    """A release that omits Identifiers.parquet must skip, not error.

    The bug this guards was invisible for the same reason the stale
    `get_curie_xref.cache_clear()` calls were: it only shows up in a run against a
    real Babel release, and those skip entirely for anyone without one configured.
    A unit test is the only place it gets exercised routinely.
    """

    @staticmethod
    def _call_fixture(downloader, tmp_path):
        """Invoke the fixture's underlying function directly, past the decorator."""
        return conftest.downloaded_identifiers.__wrapped__(downloader, str(tmp_path))

    def test_missing_file_skips(self, tmp_path):
        downloader = Mock()
        downloader.get_downloaded_file.side_effect = MissingBabelFileError(
            "This Babel release (2026jul22) does not publish duckdb/Identifiers.parquet."
        )

        with pytest.raises(Skipped) as excinfo:
            self._call_fixture(downloader, tmp_path)

        # The downloader's own message explains how to point at a release that has it.
        assert "does not publish duckdb/Identifiers.parquet" in str(excinfo.value)

    def test_present_file_is_returned(self, tmp_path):
        """The skip must not swallow the normal path."""
        downloader = Mock()
        downloader.get_downloaded_file.return_value = (
            "/cache/duckdb/Identifiers.parquet"
        )

        assert (
            self._call_fixture(downloader, tmp_path)
            == "/cache/duckdb/Identifiers.parquet"
        )

    def test_other_errors_still_propagate(self, tmp_path):
        """Only a missing file is a skip; a real failure must still fail the run."""
        downloader = Mock()
        downloader.get_downloaded_file.side_effect = RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            self._call_fixture(downloader, tmp_path)


class TestSessionFinishCleanup:
    """A unit run must not delete an integration run's multi-gigabyte download.

    `data/test` is a fixed path, not a per-run temporary directory, so the cleanup hook
    is the one piece of test infrastructure that can destroy another process's work.
    """

    @staticmethod
    def _session(markers):
        """A stub session whose items carry the given marker names."""

        def item(name):
            it = Mock()
            it.get_closest_marker.side_effect = lambda m, n=name: (
                Mock() if m == n else None
            )
            return it

        session = Mock()
        session.items = [item(m) for m in markers]
        del session.config.workerinput  # a controller, not an xdist worker
        return session

    def test_unit_only_session_leaves_the_directory_alone(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "test"
        data_dir.mkdir()
        (data_dir / "Concord.parquet").write_bytes(b"someone else is using this")
        monkeypatch.setattr(conftest, "TEST_DATA_DIR", str(data_dir))

        conftest.pytest_sessionfinish(self._session([None, None]), 0)

        assert data_dir.exists(), (
            "a `-m 'not integration'` run must not delete the integration cache"
        )

    def test_integration_session_cleans_up(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "test"
        data_dir.mkdir()
        (data_dir / "Concord.parquet").write_bytes(b"this run's own download")
        monkeypatch.setattr(conftest, "TEST_DATA_DIR", str(data_dir))

        conftest.pytest_sessionfinish(self._session([None, "integration"]), 0)

        assert not data_dir.exists(), "the next run must start fresh"

    def test_xdist_worker_never_cleans_up(self, tmp_path, monkeypatch):
        """Only the controller cleans up; a worker finishing early must not."""
        data_dir = tmp_path / "test"
        data_dir.mkdir()
        monkeypatch.setattr(conftest, "TEST_DATA_DIR", str(data_dir))

        worker = Mock()
        worker.items = [Mock()]
        worker.config.workerinput = {"workerid": "gw0"}

        conftest.pytest_sessionfinish(worker, 0)

        assert data_dir.exists()
