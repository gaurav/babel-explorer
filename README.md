# Babel Explorer
Software for querying and exploring Babel intermediate files.

babel-explorer allows you to discover why two biological/chemical identifiers are considered identical by the [Babel](https://github.com/TranslatorSRI/Babel) system, which handles cross-references between different ontology and database identifiers (e.g., MONDO, HP, UMLS, HGNC).

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for package management:

```bash
uv sync --group dev
cp env.default .env
```

## Configuration

`.env` holds the endpoints babel-explorer talks to:

| Variable | Default | Purpose |
|---|---|---|
| `BABEL_RELEASES_URL` | `https://stars.renci.org/var/babel/` | Directory holding one subdirectory per Babel release |
| `BABEL_VERSION` | `latest` | Which release subdirectory to query |
| `BABEL_LOCAL_DIR` | `data` | Where downloaded Babel files are cached |
| `BABEL_CHECK_DOWNLOAD` | `3h` | How often to re-check downloads |
| `NODENORM_URL` | `https://nodenormalization-sri.renci.org/` | NodeNorm instance for labels and cliques |
| `NAMERES_URL` | `https://name-resolution-sri.renci.org/` | Name Resolver instance for viewer autocomplete |
| `BABEL_ALLOW_VERSION_MISMATCH` | `false` | Proceed when NodeNorm reports a different Babel release than the one being queried |

Each has a matching command-line option, and precedence runs **flag > environment variable >
`.env` > default**. The release actually queried — the *effective Babel URL* — is
`BABEL_RELEASES_URL` + `BABEL_VERSION` + `/`.

`--babel-url` is the one exception. It takes a complete URL and overrides the composed pair, for a
tree that does not follow the releases-directory layout. It is **command-line only**: there is no
`BABEL_URL` environment variable, so the environment can never disagree with itself about which
release is in effect.

> **Public releases cannot serve data yet.** Public Babel releases do not currently publish the
> DuckDB Parquet files (`duckdb/Concord.parquet`, `duckdb/Identifiers.parquet`) that babel-explorer
> needs, so the shipped defaults will report that the files are missing. Translator team members
> should contact the Babel developers for the Translator-specific releases URL and set
> `BABEL_RELEASES_URL` to it in their `.env`, or pass `--babel-url <complete URL>` for a single
> run. Tracked in [#16](https://github.com/TranslatorSRI/babel-explorer/issues/16).

### Babel versions

`BABEL_LOCAL_DIR` holds one Babel release at a time. When the effective Babel URL starts pointing
at a different release, babel-explorer notices and re-downloads the files that changed — you do not
need to clear the cache by hand. The cache marker records the release the server *resolved* to, so
`BABEL_VERSION=latest` and `BABEL_VERSION=2025dec11` share a cache while they name the same
release.

`xrefs` refuses to run when NodeNorm was built from a different Babel release than the one being
queried, since the labels and cliques would not match the cross-references. Either pin
`BABEL_VERSION` to the release NodeNorm reports, point `--nodenorm-url` at a matching NodeNorm, or
pass `--allow-version-mismatch` to override.

## Usage

```bash
# Get cross-references for one or more CURIEs
uv run babel-explorer xrefs MONDO:0004979

# Get cross-references with expansion (recursive lookup)
uv run babel-explorer xrefs MONDO:0004979 --recurse

# Get cross-references with labels from NodeNorm
uv run babel-explorer xrefs MONDO:0004979 --labels
# Labels appear in double quotes immediately after the CURIE:
#   MONDO:0004979 "asthma"  skos:exactMatch  EFO:0000270 "asthma"

# Open the local clique and cross-reference graph viewer
uv run babel-explorer viewer

# Get ID records for CURIEs
uv run babel-explorer ids MONDO:0004979

# Get ID records with labels from NodeNorm
uv run babel-explorer ids MONDO:0004979 --labels

# Test concordance changes with NodeNorm
uv run babel-explorer test-concord MONDO:0004979 HP:0000001
```

The viewer accepts a CURIE directly or searches Name Resolver for a concept name. It initially
shows only identifiers in the selected identifier's NodeNorm clique and the Babel concordance
edges between them. **Show full concordance** lazily runs the recursive query, adds every reachable
identifier, and colors the added nodes by their NodeNorm clique. Edge colors and filters continue
to identify the Babel concordance source, while the inspector preserves the full source path and
predicate for each selected edge. Use `--no-open-browser` to serve without opening a browser, or
set `--host` and `--port` to change the listener.

## Testing

Tests are split into fast **unit tests** (mocked, no network) and slower **integration tests** (real file downloads and API calls), controlled by pytest markers.

Integration tests run against whatever `BABEL_RELEASES_URL` and `BABEL_VERSION` compose to, and
skip when that release does not publish the DuckDB Parquet files.

```bash
# Unit tests only — fast, no network required
uv run pytest -v -m "not integration"

# Integration tests without the Identifiers.parquet download
uv run pytest -v -m "integration and not slow"

# Full suite including large file downloads
uv run pytest -v
```

## Linting

Run both checks before committing; CI enforces them on every pull request:

```bash
uv run ruff check --fix    # lint, with auto-fix
uv run ruff format         # format
```

### Adding Test CURIEs

Integration tests are parametrized over the CURIEs listed in `tests/data/valid_curies.txt`. Add a new CURIE on its own line to automatically expand test coverage:

```
# tests/data/valid_curies.txt
MONDO:0004979
HP:0000001
```
