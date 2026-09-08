# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` points here, so Claude Code
reads it too.

## Project Overview

babel-explorer is a CLI for asking Babel **why** it considers two identifiers the same thing. It
reads Babel's intermediate Parquet files through DuckDB and, optionally, enriches the results
with labels from NodeNorm. It is a read-only diagnostic tool: it never writes to a Babel release
and never changes what Babel decided.

## Domain context

If you have not worked in the Translator ecosystem, read this before the code — the terms below are
used throughout it without explanation.

**Babel** builds *cliques*: equivalence sets of identifiers across biomedical vocabularies,
"recognizing that MESH:D014867 and DRUGBANK:DB09145 both refer to water". A clique carries a
**preferred identifier**, a **Biolink type**, and the list of equivalent identifiers. Which member
becomes preferred is decided by the `id_prefixes` order in the Biolink Model, not by this tool.

**Concords** are the asserted cross-reference edges that *feed* clique building — the inputs, not
the output. This distinction is the whole point of babel-explorer, and it cuts both ways:

> Babel's own `AGENTS.md` says to answer clique-membership questions "from a finished build — the
> compendia themselves, the DuckDB `Edge` table, or Node Normalization — never from the concords
> that fed it."

So `xrefs` answers **"what did Babel read that made it think these are the same?"** — which is
exactly what you want when a merge looks wrong. It does *not* tell you what Babel finally decided.
For that, ask NodeNorm: `--labels` and `test-concord` do. Do not "improve" `xrefs` into a clique
oracle; it is deliberately reporting the evidence, not the verdict.

**NodeNorm** (the NodeNormalization service) serves the finished cliques: give it a CURIE, get back
the preferred CURIE, the equivalent identifiers, and Biolink types. Its data "is created by
Babel" — each deployment is built from one specific Babel release, which is why a version skew
between it and the Parquet files matters, and why `/status` reporting `babel_version` is what this
tool checks. **Conflation** merges cliques that are distinct but usually wanted together;
`nodenorm.py` requests both kinds on every call (`conflate` for GeneProtein,
`drug_chemical_conflate` for DrugChemical).

### What this tool reads

A Babel release is a dated directory (`2026jul22`). This tool uses three files from `duckdb/`
inside one:

| File | Contents |
|---|---|
| `Concord.parquet` | One row per asserted cross-reference: `filename`, `subj`, `pred`, `obj`. `filename` is the concord file the edge came from, which is why `xrefs` can show provenance. |
| `Identifiers.parquet` | Per-identifier records; schema varies by release, so `IdentifierRecord` reads columns dynamically. |
| `Metadata.parquet` | Small; downloaded alongside Concord by the integration fixtures. |

**These three files are not documented upstream** — neither Babel's `README.md` nor its
`releases/ARTIFACTS.md` describes them. The schema above is what *this* repository's code assumes,
verified against real files, not a contract Babel has published. Treat a release that disagrees as
a real possibility rather than a bug in the reader, and see [Testing](#testing): a release can
publish `Concord.parquet` without `Identifiers.parquet`.

### Upstream repositories

- **[NCATSTranslator/Babel](https://github.com/NCATSTranslator/Babel)** — builds the cliques and
  the files this tool reads. Its `AGENTS.md` is the best short account of the pipeline and of how
  preferred CURIEs are chosen.
- **[NCATSTranslator/NodeNormalization](https://github.com/NCATSTranslator/NodeNormalization)** —
  the service behind `--labels`, `test-concord` and the version check.
- **[NCATSTranslator/NameResolution](https://github.com/NCATSTranslator/NameResolution)** — name →
  CURIE lookup, built on NodeNorm. Not used by this tool; listed because it is the third service
  people expect to find alongside the other two.

Release naming, `latest`, and `VERSION.txt` are conventions this tool relies on but that are not
documented in any of those repositories — see [Babel versions](#babel-versions) for what it
actually does with them.

## Development Setup

This project uses **uv** for package management:

```bash
# Install dependencies
uv sync

# Install with dev dependencies
uv sync --group dev

# Configure the Babel and NodeNorm endpoints
cp env.default .env

# Run the CLI
uv run babel-explorer --help
```

## Configuration

`BABEL_RELEASES_URL`, `BABEL_VERSION`, `BABEL_LOCAL_DIR`, `BABEL_CHECK_DOWNLOAD`, `NODENORM_URL`, and
`BABEL_ALLOW_VERSION_MISMATCH` are read from `.env` (via `python-dotenv`, loaded in the `cli()`
group) or the environment. Each is also a command-line option, and precedence runs
**flag > environment variable > `.env` > built-in default**.

The release actually queried — the **effective Babel URL** — is
`BABEL_RELEASES_URL.rstrip("/") + "/" + BABEL_VERSION + "/"`, composed by `resolve_babel_url()`
(`cli.py`) on top of the pure `compose_babel_url()` (`core/downloader.py`). `compose_babel_url`
lives in the downloader rather than the CLI because `tests/constants.py` needs the same
composition and must not import Click to get it.

`--babel-url` overrides the composed pair with a complete URL, for a tree that does not follow the
releases-directory layout. It has **no `envvar=`, on purpose**: two variables already feed the
composed URL, and a third that silently outranked both would make "which release am I querying?"
unanswerable from the environment alone. Do not add one. `BABEL_URL` was the single pre-refactor
setting and is now inert; `cli()` warns if it is still set so it does not fail silently.

`env.default` ships with the **public** Babel URL only. Public Babel releases do not currently
publish the DuckDB Parquet files this tool needs, so Translator team members must contact the
Babel developers for the Translator-specific releases URL and set `BABEL_RELEASES_URL` to it.
Never commit that URL to this repository.

## Babel versions

The Babel version behind the effective Babel URL is resolved by `resolve_babel_version()`
(`core/downloader.py`), which reads `VERSION.txt` (`Babel 2026jul22`) and falls back to the final
path segment for older trees that predate it. `latest/` resolves to whatever release it currently
points at. That fallback segment is now exactly `BABEL_VERSION`, so a pinned release still
resolves when `VERSION.txt` is unreachable, while `latest` yields `None` as before.

`BABEL_LOCAL_DIR` holds **one Babel release at a time**, recorded in a `.babel-version` marker.
When the release changes, the downloader re-validates every cached file against the new release and
re-downloads only what actually changed — the multi-gigabyte Parquet files are never deleted
speculatively. Downloads land in a sibling `.tmp` and are promoted with `os.replace`, so a partial
file is never visible as the real one.

Those rules are load-bearing and each records a specific failure — a corrupt or cross-release
Parquet that then passes every freshness check, permanently. See
**[docs/Downloading.md](docs/Downloading.md)** before changing anything in `core/downloader.py`,
`sync_cache_version()`, `_is_within_freshness()` or the resume path.

`BabelExplorerGroup.invoke()` turns `requests.RequestException` into a `ClickException`, so an
unreachable NodeNorm reports an error rather than a traceback. In practice only NodeNorm reaches
it: the downloader handles its own network failures, while NodeNorm deliberately lets HTTP errors
propagate so a failed lookup is not cached. `get_babel_version()` swallows its own errors, so an
unreachable NodeNorm passes the version check below and only fails part-way through the query.

`xrefs` fails when NodeNorm's `status` endpoint reports a different `babel_version` than the Babel
being queried, since labels and cliques would not match the cross-references. Pass
`--allow-version-mismatch` to proceed anyway. The check is skipped when NodeNorm is not consulted
(`xrefs` without `--labels`, including `--recurse`, which is served entirely by DuckDB; and `ids`
without `--labels`) or when either version is unavailable.

## Commands

### Running the Application

`README.md` documents every command and flag with worked examples; it is the user-facing reference
and is kept current. Do not restate it here — this file covers what an agent needs *beyond* the
README.

### Development Commands

```bash
# Run all tests (includes large file downloads)
uv run pytest -v

# Run unit tests only (fast, no network)
uv run pytest -v -m "not integration"

# Run integration tests without the Identifiers.parquet download
uv run pytest -v -m "integration and not slow"

# Run a single test file
uv run pytest -v tests/test_nodenorm.py

# Run serially, e.g. to read one test's output
uv run pytest -v -n0 tests/test_nodenorm.py
```

`[tool.pytest.ini_options]` puts `-n auto` in `addopts`, so every run is parallel by default.
Disable it with `-n0`, **not** `-p no:xdist` — unloading the plugin leaves the already-parsed
`-n` behind and pytest exits with `unrecognized arguments: -n`.

### Linting

**Run both of these before committing or pushing.** CI checks them on every PR, and a push
that skips them turns the PR red for reasons unrelated to the change under review.

```bash
uv run ruff check          # Python lint
uv run ruff check --fix    # Python auto-fix
uv run ruff format --check # Python format check
uv run ruff format         # Python auto-format
```

Run them over the whole repository, not just the files you touched — `[tool.ruff]` in
`pyproject.toml` sets the scope. If `ruff format` reports files you did not edit, the repository
had drifted; commit that reformatting separately from your change so review stays readable, and
do not silently revert it.

Rules are `E`, `F`, `I` (import sorting) and `UP` (pyupgrade), with `E501` left to the formatter.
Line length is ruff's default of 88. `*.md` is excluded, because ruff 0.16+ reformats Python
inside Markdown code blocks and this repository's snippets are illustrative fragments.

## Console Output Format Conventions

### Label display

When a human-readable label is shown alongside a CURIE in console output, it always appears **immediately after the CURIE, in double quotes**:

```
MONDO:0004979 "asthma"  skos:exactMatch  EFO:0000270 "asthma"
```

This applies everywhere labels appear: `xrefs --labels`, `xrefs --paths --labels`, `ids --labels`, and `test-concord`.

`--paths` is console-only; combining it with `--format json`/`tsv`/`csv` is rejected up front rather
than silently emitting the full cross-reference list. It also needs at least two CURIEs, and that
is checked in the same place, before `make_downloader()` — `--paths` implies `--recurse`, so
finding out inside `_print_paths()` would cost a multi-gigabyte download and a full recursive query
before rejecting the run.

**When a label is absent, omit it entirely** — do not substitute a placeholder like `-` or `""`. A CURIE with no label renders as just the bare CURIE.

**Escaping:** embedded backslashes are escaped as `\\` and embedded double quotes as `\"`. Downstream tools can parse labels with the regex `"([^"\\]|\\.)*"`.

**Do not** use parentheses `(label)` or any other delimiter — double quotes are the sole convention.

## Architecture

### Core Components

1. **BabelDownloader** (`src/babel_explorer/core/downloader.py`):
   - Downloads Babel intermediate files from a remote HTTP(S) server using Python's `requests` library (streaming downloads)
   - Caches files locally in a configurable directory (default: `data/`), one Babel release at a time
   - Caching is on disk, keyed by ETag and the `.meta` sidecars — not in memory. Only
     `babel_version` is memoised, via `functools.cached_property`
   - Resolves the Babel version (`resolve_babel_version`) and refreshes the cache when it changes (`sync_cache_version`)
   - Raises `MissingBabelFileError` on **any** 404, not just `duckdb/` paths, so a release that
     omits a file it is asked for reports that rather than a bare HTTP error

2. **BabelXRefs** (`src/babel_explorer/core/babel_xrefs.py`):
   - Main query engine for cross-references
   - Uses DuckDB to query Parquet files (`Concord.parquet`, `Identifiers.parquet`)
   - Supports recursive expansion of cross-references via a single `WITH RECURSIVE` query
   - Uses ephemeral in-memory DuckDB connections, opened by `BabelXRefs._connect()`. No
     database is persisted, but larger-than-memory queries spill to disk: `_connect()` sets
     `temp_directory` to `<BABEL_LOCAL_DIR>/duckdb-spill/`, because DuckDB's default is
     `.tmp` in the *current working directory* and a `--recurse` run materialises the whole
     multi-gigabyte Concord relation

3. **NodeNorm** (`src/babel_explorer/core/nodenorm.py`):
   - Integration with NodeNormalization API (https://nodenormalization-sri.renci.org/)
   - Fetches labels, biolink types, and equivalent identifiers for CURIEs
   - Caches normalisation results, identifiers and cliques in per-instance dicts; a new
     `NodeNorm` object is the way to get uncached results
   - `get_babel_version()` reads the `status` endpoint to report which Babel release it was built from
   - Optional component for label enrichment

4. **CLI** (`src/babel_explorer/cli.py`):
   - Click-based command-line interface
   - Three main commands: `xrefs`, `ids`, `test-concord`

### Data Flow

1. User provides CURIEs via CLI; `BABEL_RELEASES_URL` + `BABEL_VERSION` / `NODENORM_URL` come from `.env` or the environment, and are composed into the effective Babel URL
2. BabelDownloader resolves the Babel version, refreshes the cache if it changed, and ensures required Parquet files are downloaded
3. BabelXRefs queries files using DuckDB
4. If `--labels` is set, NodeNorm is queried for additional metadata (`--recurse` alone does not consult NodeNorm — the recursive expansion is a single DuckDB query)
5. Results are printed to stdout

### Key Design Patterns

- **Lazy downloading**: files are only downloaded when first accessed
- **Recursive expansion**: `--recurse` follows cross-references transitively in a single
  `WITH RECURSIVE` DuckDB query rather than a Python loop

## Testing

### Test Structure

Tests live in `tests/` and are split into fast **unit tests** (mocked, no network) and slower **integration tests** (real downloads and API calls). Pytest markers control which tests run:

- **`@pytest.mark.integration`** — requires network access (downloads Parquet files or calls NodeNorm API)
- **`@pytest.mark.slow`** — downloads `Identifiers.parquet`, the largest file Babel publishes

Note that `not slow` is *not* the same as "small". `Concord.parquet` is itself multi-gigabyte in
current releases (4.6 GB in `2026jul22`) and its tests are not marked slow, because excluding them
would leave the non-slow integration set covering nothing that touches real data. Budget for that
before pointing CI at a Babel that publishes the Parquet files (see #18 and #28).

Do not record per-file test counts here — they drift silently and then mislead. Get them on demand:

```bash
uv run pytest --collect-only -q -m "not integration"   # unit test count
uv run pytest --collect-only -q                        # full count
```

**Integration tests skip when the composed Babel URL points at a release that does not publish
`duckdb/Concord.parquet`**, which is the case for every public release right now. A run reporting
a couple of dozen skips is the expected result without a Translator `BABEL_RELEASES_URL` in `.env`, not a
broken test environment.

**A release can publish one DuckDB file without the other.** `2026jul22` serves a 4.6 GB
`Concord.parquet` with no `Identifiers.parquet` beside it, so "does this release have the Parquet
files?" is not one question. Every DuckDB file goes through `_download_or_skip()`, which skips on
the `MissingBabelFileError` the downloader already raises on a 404.
`shared_downloader` answers only the genuinely session-wide question — is the server reachable —
and deliberately probes the release root rather than a specific file, so it cannot be mistaken for
a publication check. Do not collapse these back into one up-front probe: the non-slow integration
tests run perfectly well against a release that lacks `Identifiers.parquet`.

**A full run re-downloads everything.** `pytest_sessionfinish` deletes `data/test`, so each
`uv run pytest` with a Babel release configured pays the multi-gigabyte download again (~9 minutes
for `2026jul22`). Use `-m "not integration"` while iterating, and budget for the full run.

**Only one pytest session may touch `data/test` at a time.** It is a fixed path shared by every
run, not a per-run temporary directory, and `pytest_sessionfinish` deletes it at the end of the
session. The `FileLock` around each download does not help: it guards one file, not a session.

`pytest_sessionfinish` skips the cleanup when the session selected no `integration` tests, so the
fast `-m "not integration"` loop you run while editing is safe alongside a long integration run.
**Do not remove that guard.** Without it, every unit run — the most frequent command in this
repository — silently deletes a multi-gigabyte download in progress in another terminal, and the
integration run just starts over, looking like a downloader bug rather than an unrelated `pytest`
invocation two windows away. This was diagnosed twice as a resume defect before the real cause
turned up.

Two concurrent *integration* runs still clash, and nothing here prevents that:

- **Detect it**: `du -sh data/test` going *down* instead of up, and a run sailing well past the ~9
  minutes it should take. `ps -eo pid,etime,command | grep "[p]ytest"` showing two sets of workers
  with different `etime` values confirms it.
- **Avoid it**: check for a run already in flight before starting one, and never `rm -rf data/test`
  to "start clean" without checking first — that is the fastest way to corrupt a run in progress.
- **Recover**: kill every pytest process, `rm -rf data/test`, and start exactly one run. Nothing
  outside `data/test` is affected, so no repository or cache state needs repairing.

### Test Infrastructure

- **`tests/conftest.py`** — Session-scoped fixtures that download Parquet files once and share them across all integration tests. `shared_downloader` HEADs the release root and skips the session if the server is unreachable; `_download_or_skip()` then skips per file if the release does not publish it. `nodenorm` probes `status` the same way, so a NodeNorm outage skips rather than reddening CI. Teardown removes the `data/test/` directory so the next run starts fresh — see the concurrency warning above before running two suites at once.
- **`tests/constants.py`** — Shared constants (URLs, file paths) and `load_curies()` helper.
- **`tests/data/valid_curies.txt`** — One CURIE per line (`#` comments allowed). Integration tests are parametrized over this list — adding a new line automatically expands test coverage.

### Key Dataclasses

- **`Identifier`** — Frozen dataclass for a normalized NodeNorm entry (curie, label, biolink_type, taxa, description). Returned by `NodeNorm.get_identifier()` and `get_clique_identifiers()`.
- **`CrossReference`** — Frozen dataclass for Concord.parquet rows (filename, subj, pred, obj)
- **`LabeledCrossReference`** — Extends CrossReference with labels and biolink types from NodeNorm
- **`IdentifierRecord`** — Frozen dataclass for Identifiers.parquet rows (curie + dynamic extra fields, plus `nodenorm_label` under `--labels`). Returned by `BabelXRefs.get_curie_ids()`. The NodeNorm label is *not* called `label`: Identifiers.parquet has its own `label` column, which lands in `extra_fields` and would collide with it once the record is flattened for json/tsv/csv.

## Repository history was rewritten on 2026-09-01

Every commit was rewritten to remove an internal Babel URL that had been the hardcoded default
since the initial commit. Consequences a future contributor will trip over:

- **A clone taken before that date has divergent history.** Every SHA changed except `gh-pages`.
  Re-clone; do not try to merge or rebase the old history back together.
- **PRs #1, #4, #6, #7 and #11 are dead.** GitHub refuses to reopen a PR whose original head
  commits no longer exist, so they were recreated as #20-#24 (#20 has since merged; the rest are
  still open). Old PR links and commit SHAs in issue comments point at nothing.
- **The rule in [Configuration](#configuration) is now enforced by a test.**
  `TestCommittedConfigTemplate` (`tests/test_cli.py`) fails if a non-public host appears in
  `env.default`. The URL leaked in the first place because it was a default in source rather than
  configuration, so do not weaken that test to accommodate a convenient default.

## File Locations

- Source code: `src/babel_explorer/`
- Tests: `tests/`
- Test CURIEs: `tests/data/valid_curies.txt`
- Downloaded Babel files: `<BABEL_LOCAL_DIR>/duckdb/*.parquet` (default `data/duckdb/`)
- DuckDB query spill: `<BABEL_LOCAL_DIR>/duckdb-spill/` (default `data/duckdb-spill/`)
- Endpoint configuration: `.env` (gitignored), template in `env.default`
- Entry point: `src/babel_explorer/cli.py`
