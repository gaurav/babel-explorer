# Changelog

All notable changes to babel-explorer are documented here. This project follows
[semantic versioning](https://semver.org/).

## 0.1.0 — 2026-09-01

First release. A Click CLI for querying Babel intermediate files through DuckDB, with optional
label enrichment from NodeNorm.

### Added

- `xrefs` — cross-references for one or more CURIEs, with `--recurse` for transitive expansion
  (a single `WITH RECURSIVE` DuckDB query), `--paths` for the shortest paths connecting the given
  CURIEs, and `--labels` for NodeNorm labels and Biolink types.
- `ids` — identifier records from `Identifiers.parquet`, with `--labels`.
- `test-concord` — compare a proposed concordance change against NodeNorm's current cliques.
- `viewer` — local interactive graph viewer with Name Resolver autocomplete, a clique-first
  NodeNorm view, lazy recursive Babel expansion, clique-colored added nodes, NodeNorm labels,
  and source-specific edge colors and filters.
- `--format json|tsv|csv` on `xrefs` and `ids` for machine-readable output.
- `BabelDownloader`: streaming downloads with ETag-based freshness checking, resumable retries,
  and a cache that holds one Babel release at a time and refreshes itself when that release
  changes.
- Configuration from `.env` or the environment — `BABEL_RELEASES_URL`, `BABEL_VERSION`,
  `BABEL_LOCAL_DIR`, `BABEL_CHECK_DOWNLOAD`, `NODENORM_URL`, `NAMERES_URL`,
  `BABEL_ALLOW_VERSION_MISMATCH` — with `env.default` as the committed template. Precedence:
  flag > environment > `.env` > default.
- A version-skew check that refuses to mix labels from one Babel release with cross-references
  from another, overridable with `--allow-version-mismatch`.

### Known limitations

- **The shipped defaults cannot query data yet.** Public Babel releases do not publish
  `duckdb/Concord.parquet` or `duckdb/Identifiers.parquet`. Translator team members can set
  `BABEL_RELEASES_URL` to an internal releases URL; everyone else gets a clear error rather than
  results. Tracked in [#16](https://github.com/TranslatorSRI/babel-explorer/issues/16).
- **`--labels` fails against the public defaults.** NodeNorm dev reports Babel `2025sep1` while
  public `latest` is `2025dec11`, so the skew check fires. Pin `BABEL_VERSION` to the release
  NodeNorm was built from, or pass `--allow-version-mismatch`. Tracked in
  [#17](https://github.com/TranslatorSRI/babel-explorer/issues/17).
- The integration tests skip without a Babel release publishing the Parquet files, so a default
  run exercises only the unit suite. Tracked in
  [#18](https://github.com/TranslatorSRI/babel-explorer/issues/18).
