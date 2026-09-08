"""Command-line interface for babel-explorer."""

import logging
import os
import re
from itertools import combinations

import click
import requests
from dotenv import load_dotenv
from rich.markup import escape

from babel_explorer.core.babel_xrefs import (
    BabelXRefs,
    LabeledCrossReference,
    build_adjacency,
    build_depth_map,
    find_shortest_path,
)
from babel_explorer.core.downloader import (
    BabelDownloader,
    MissingBabelFileError,
    compose_babel_url,
)
from babel_explorer.core.nameres import NameResolver
from babel_explorer.core.nodenorm import NodeNorm
from babel_explorer.formatting import (
    curie_with_label,
    format_identifier_record,
    hl_curie,
    make_console,
    record_to_dict,
    write_records,
)
from babel_explorer.viewer import ViewerService, serve_viewer


def _validate_babel_version(ctx, param, value):
    """Reject a --babel-version that is really a URL or a path traversal.

    Anyone with muscle memory from the old BABEL_URL variable will eventually put a
    complete URL here, which would compose into nonsense with no useful diagnostic.
    """
    if value is None:
        return value
    if "://" in value:
        raise click.BadParameter(
            f"{value!r} looks like a complete URL. Pass it to --babel-url instead, "
            f"or give --babel-version just the release name (e.g. '2025dec11')."
        )
    if ".." in value:
        raise click.BadParameter(f"{value!r} may not contain '..'.")
    return value


def babel_options(f):
    """Decorator adding the Babel source options: --local-dir, --babel-releases-url,
    --babel-version, --babel-url, --check-download and --allow-version-mismatch."""
    f = click.option(
        "--allow-version-mismatch",
        is_flag=True,
        envvar="BABEL_ALLOW_VERSION_MISMATCH",
        help="Proceed even if NodeNorm was built from a different Babel release than "
        "the one being queried",
    )(f)
    f = click.option(
        "--check-download",
        type=str,
        default="3h",
        show_default=True,
        envvar="BABEL_CHECK_DOWNLOAD",
        help="How often to re-check downloads (e.g. '3h', '30m', '1d', '0', 'never'). "
        "'never' disables re-checking and always uses cached files; '0' forces a re-check every time.",
    )(f)
    f = click.option(
        "--babel-url",
        type=str,
        default=None,
        # Deliberately NO envvar=. BABEL_RELEASES_URL + BABEL_VERSION is the only
        # environment-driven path to a Babel URL, so there is never a question of which
        # variable wins. This is a per-run escape hatch, not configuration. Do not add one.
        help="Complete URL of one Babel release, overriding --babel-releases-url and "
        "--babel-version. Command line only: there is no BABEL_URL environment variable. "
        "[default: --babel-releases-url + --babel-version]",
    )(f)
    f = click.option(
        "--babel-version",
        type=str,
        default="latest",
        show_default=True,
        show_envvar=True,
        envvar="BABEL_VERSION",
        callback=_validate_babel_version,
        help="Babel release to use: the name of a subdirectory under --babel-releases-url "
        "(e.g. '2025dec11'). 'latest' follows whatever the server currently publishes.",
    )(f)
    f = click.option(
        "--babel-releases-url",
        type=str,
        default="https://stars.renci.org/var/babel/",
        show_default=True,
        show_envvar=True,
        envvar="BABEL_RELEASES_URL",
        help="URL of a directory holding one subdirectory per Babel release.",
    )(f)
    f = click.option(
        "--local-dir",
        type=str,
        default="data",
        show_default=True,
        show_envvar=True,
        envvar="BABEL_LOCAL_DIR",
        help="Local location to save Babel download files to. Holds one Babel release at "
        "a time; cached files are refreshed automatically when the effective Babel URL "
        "points at a new one.",
    )(f)
    return f


def nodenorm_options(f):
    """Decorator adding --nodenorm-url to a command."""
    return click.option(
        "--nodenorm-url",
        type=str,
        default="https://nodenormalization-sri.renci.org/",
        show_default=True,
        envvar="NODENORM_URL",
        help="NodeNorm base URL used for node normalization and label enrichment",
    )(f)


def nameres_options(f):
    """Decorator adding --nameres-url to a command."""
    return click.option(
        "--nameres-url",
        type=str,
        default="https://name-resolution-sri.renci.org/",
        show_default=True,
        envvar="NAMERES_URL",
        help="Name Resolver base URL used for name autocomplete",
    )(f)


def resolve_babel_url(
    babel_url: str | None, babel_releases_url: str, babel_version: str
) -> str:
    """The effective Babel URL: the one release this run will query.

    ``--babel-url`` is a complete URL and wins outright; otherwise the release is
    composed from the releases directory and the version. ``--babel-url`` has no
    matching environment variable on purpose — with two variables already feeding the
    composed URL, a third that silently outranked both would make "which release am I
    actually querying?" unanswerable from the environment alone.
    """
    if babel_url:
        # Warn only when --babel-version was actually typed. A developer with
        # BABEL_VERSION permanently in .env would otherwise be warned on every
        # --babel-url run, which just teaches them to ignore warnings.
        ctx = click.get_current_context(silent=True)
        if ctx is not None and ctx.get_parameter_source("babel_version") == (
            click.core.ParameterSource.COMMANDLINE
        ):
            click.echo(
                f"Warning: --babel-url overrides --babel-version, so "
                f"{babel_version!r} is ignored.",
                err=True,
            )
        return babel_url.strip().rstrip("/") + "/"
    return compose_babel_url(babel_releases_url, babel_version)


def make_downloader(
    babel_url: str | None,
    babel_releases_url: str,
    babel_version: str,
    local_dir: str,
    check_download: str,
):
    """Build a BabelDownloader and point its cache at the effective Babel release.

    Composition happens here rather than at each call site so a future command cannot
    take the options and forget to resolve them.
    """
    downloader = BabelDownloader(
        resolve_babel_url(babel_url, babel_releases_url, babel_version),
        local_path=local_dir,
        freshness_seconds=parse_duration(check_download),
    )
    downloader.sync_cache_version()
    return downloader


def check_babel_versions(
    downloader: BabelDownloader, nodenorm: NodeNorm, allow_version_mismatch: bool
):
    """Fail if NodeNorm was built from a different Babel release than the one being queried.

    Cross-release results are silently wrong rather than obviously wrong: labels and
    cliques would come from one Babel while the cross-references come from another.
    Skipped when either version is unavailable.
    """
    # Named for the release the *server* reports, to keep it distinct from the
    # babel_version parameter the commands take, which is the release the user asked for.
    downloader_version = downloader.babel_version
    nodenorm_version = nodenorm.get_babel_version()
    if (
        downloader_version
        and nodenorm_version
        and downloader_version != nodenorm_version
        and not allow_version_mismatch
    ):
        raise click.ClickException(
            f"NodeNorm at {nodenorm.nodenorm_url} was built from Babel {nodenorm_version}, "
            f"but {downloader.url_base} is Babel {downloader_version}. Labels and cliques would "
            f"not match the cross-references. Point --nodenorm-url at a matching NodeNorm, "
            f"pin --babel-version to the release NodeNorm was built from, or pass "
            f"--allow-version-mismatch to proceed anyway."
        )


def format_option(f):
    """Decorator adding --format and --json-indent options to a command."""
    f = click.option(
        "--format",
        "fmt",
        default="console",
        type=click.Choice(["console", "json", "tsv", "csv"]),
        show_default=True,
        help="Output format",
    )(f)
    f = click.option(
        "--json-indent",
        default=2,
        show_default=True,
        help="Indentation depth for JSON output",
    )(f)
    return f


#: Duration suffixes accepted by --check-download; no suffix means seconds.
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int | float:
    """Parse a duration string like '3h', '30m', '1d', '7200', or 'never' → seconds."""
    lower = (value or "").strip().lower()
    if lower == "never":
        return float("inf")
    # The pattern rejects empty, negative and non-integer values in one go, so there
    # is a single wording of the error rather than one per rejected shape.
    match = re.fullmatch(r"(\d+)([smhd]?)", lower)
    if not match:
        raise click.BadParameter(
            f"Invalid duration {value!r}: expected a non-negative integer number of "
            "seconds, optionally followed by 's', 'm', 'h', or 'd', or 'never'."
        )
    return int(match[1]) * _DURATION_UNITS[match[2]]


def _depth_of(curie: str, query_set: set, depth: int | None) -> int | None:
    """Depth to render a CURIE at: query CURIEs are always depth 0."""
    return 0 if curie in query_set else depth


def _print_paths(console, curies, xrefs_list, labels: bool) -> None:
    """Print the shortest path between every pair of query CURIEs.

    The caller checks the "at least two CURIEs" requirement up front — see ``xrefs`` —
    so that a run that cannot produce a path never downloads Concord.parquet to find
    that out. With fewer than two, ``combinations`` simply yields no pairs.
    """
    curie_list = list(curies)
    query_set = set(curie_list)
    # One neighbour map for every pair: rebuilding it per pair re-walks the whole
    # recursive xref list C(n,2) times.
    adj = build_adjacency(xrefs_list)

    for from_c, to_c in combinations(curie_list, 2):
        path = find_shortest_path(from_c, to_c, xrefs_list, adj)
        header_from = hl_curie(from_c, 0)
        header_to = hl_curie(to_c, 0)

        if path is None:
            console.print(
                f"[bold]Path:[/bold] {header_from} [dim]→[/dim] {header_to}"
                f"  [red]no path found[/red]"
            )
            console.print()
            continue

        if len(path) == 0:
            console.print(
                f"[bold]Path:[/bold] {header_from} [dim]=[/dim] {header_to}"
                f"  [dim](same node)[/dim]"
            )
            console.print()
            continue

        # Reconstruct ordered node list from the edge sequence.
        nodes = [from_c]
        for edge in path:
            prev = nodes[-1]
            nodes.append(edge.obj if edge.subj == prev else edge.subj)

        # Header: node1 → node2 → … → nodeN. Position along the path is the depth
        # from from_c, except for query CURIEs, which always render as depth 0.
        node_strs = [
            hl_curie(node, _depth_of(node, query_set, i))
            for i, node in enumerate(nodes)
        ]
        n_steps = len(path)
        step_word = "step" if n_steps == 1 else "steps"
        console.print(
            f"[bold]Path ({n_steps} {step_word}):[/bold] "
            + " [dim]→[/dim] ".join(node_strs)
        )

        # Edge details, indented, oriented in traversal direction.
        for i, edge in enumerate(path):
            from_node = nodes[i]
            subj_node = edge.subj if edge.subj == from_node else edge.obj
            obj_node = edge.obj if edge.subj == from_node else edge.subj

            subj_label = obj_label = None
            if labels and isinstance(edge, LabeledCrossReference):
                if edge.subj == subj_node:
                    subj_label = edge.subj_label
                    obj_label = edge.obj_label
                else:
                    subj_label = edge.obj_label
                    obj_label = edge.subj_label

            subj_str = curie_with_label(
                subj_node, _depth_of(subj_node, query_set, i), subj_label
            )
            obj_str = curie_with_label(
                obj_node, _depth_of(obj_node, query_set, i + 1), obj_label
            )

            console.print(
                f"  - {subj_str}  [dim]{escape(edge.pred)}[/dim]  "
                f"{obj_str}  [dim italic]{escape(edge.filename)}[/dim italic]"
            )

        console.print()


class BabelExplorerGroup(click.Group):
    """Group that reports service failures as a plain error rather than a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except MissingBabelFileError as e:
            raise click.ClickException(str(e)) from e
        except requests.RequestException as e:
            # In practice this is always NodeNorm: the downloader handles its own
            # network failures (a failed HEAD falls back to the cached file, and
            # _download_with_retry re-raises as RuntimeError after its last attempt),
            # while NodeNorm deliberately lets HTTP errors propagate so a failed
            # lookup is not cached. Note that get_babel_version() swallows its own
            # errors, so an unreachable NodeNorm passes the version check and only
            # fails here, part-way through a query.
            raise click.ClickException(
                f"NodeNorm request failed: {e}. Check that --nodenorm-url is "
                f"reachable; `xrefs` and `ids` can also be run without --labels, "
                f"which does not consult NodeNorm at all."
            ) from e


@click.group(cls=BabelExplorerGroup)
def cli():
    """babel-explorer: query and explore Babel intermediate files."""
    logging.basicConfig(level=logging.INFO)
    # Runs before subcommand parameters are parsed, so .env feeds the envvar= defaults.
    load_dotenv()
    # BABEL_URL was the single Babel setting before BABEL_RELEASES_URL + BABEL_VERSION.
    # It is now inert, and silently ignoring it would send someone to the wrong release
    # with no clue why. Checked after load_dotenv() so a stale .env is caught too.
    if os.environ.get("BABEL_URL"):
        click.echo(
            "Warning: BABEL_URL is no longer used. Set BABEL_RELEASES_URL and "
            "BABEL_VERSION instead, or pass --babel-url for a single run.",
            err=True,
        )


@cli.command("xrefs")
@click.argument("curies", type=str, required=True, nargs=-1)
@babel_options
@nodenorm_options
@click.option("--recurse", is_flag=True, help="Recursively query returned xrefs")
@click.option("--labels", is_flag=True, help="Include labels for CURIEs")
@click.option(
    "--paths",
    is_flag=True,
    help="Show shortest path(s) connecting the given CURIEs (implies --recurse)",
)
@format_option
def xrefs(
    curies: list[str],
    babel_url: str | None,
    babel_releases_url: str,
    babel_version: str,
    nodenorm_url: str,
    local_dir: str,
    recurse: bool,
    labels: bool,
    paths: bool,
    check_download: str,
    allow_version_mismatch: bool,
    fmt: str,
    json_indent: int,
):
    """
    Fetches and prints the cross-references (xrefs) for the given CURIEs.

    \f

    :param curies: A list of CURIEs (Compact URI) for which cross-references need
        to be retrieved.
    :type curies: list[str]
    :param babel_url: Complete URL of one Babel release, overriding the two below.
        ``None`` unless ``--babel-url`` was passed.
    :type babel_url: str | None
    :param babel_releases_url: URL of a directory holding one subdirectory per release.
    :type babel_releases_url: str
    :param babel_version: Which release subdirectory to query, or ``latest``.
    :type babel_version: str

    :return: None
    """
    if paths:
        # Both checks happen before anything is downloaded. --paths implies --recurse,
        # so getting one of them wrong otherwise costs a multi-gigabyte Concord.parquet
        # download and a full recursive query before the run is rejected.
        #
        # Only the console renderer knows how to lay out paths; the other formats would
        # silently emit the full recursive xref list instead, which looks like a
        # successful --paths run but is not one.
        if fmt != "console":
            raise click.UsageError(
                f"--paths is only supported with --format console, not --format {fmt}. "
                f"Drop --paths to emit the full cross-reference list as {fmt}."
            )
        if len(curies) < 2:
            raise click.UsageError(
                "--paths needs at least two CURIEs to find a path between. "
                "Drop --paths to list the cross-references of a single CURIE."
            )
        recurse = True

    downloader = make_downloader(
        babel_url, babel_releases_url, babel_version, local_dir, check_download
    )
    nodenorm = NodeNorm(nodenorm_url)
    # NodeNorm is only consulted for labels; --recurse is served entirely by the
    # recursive DuckDB query, so its results cannot disagree with NodeNorm's release.
    if labels:
        check_babel_versions(downloader, nodenorm, allow_version_mismatch)

    bxref = BabelXRefs(downloader, nodenorm)
    xref_list = bxref.get_curie_xrefs(curies, recurse, label_curies=labels)

    if fmt == "console":
        console = make_console()
        if paths:
            _print_paths(console, curies, xref_list, labels)
        else:
            query_set = set(curies)
            # Without --recurse every result is one hop from a query CURIE, so there
            # is no depth to show: only the query CURIEs themselves are highlighted.
            depth_map = build_depth_map(list(curies), xref_list) if recurse else {}
            for xref in xref_list:
                labeled = isinstance(xref, LabeledCrossReference)
                subj_str = curie_with_label(
                    xref.subj,
                    _depth_of(xref.subj, query_set, depth_map.get(xref.subj)),
                    xref.subj_label if labeled else None,
                )
                obj_str = curie_with_label(
                    xref.obj,
                    _depth_of(xref.obj, query_set, depth_map.get(xref.obj)),
                    xref.obj_label if labeled else None,
                )
                console.print(
                    f"{subj_str}  [dim]{escape(xref.pred)}[/dim]  "
                    f"{obj_str}  [dim italic]{escape(xref.filename)}[/dim italic]"
                )
    else:
        write_records(xref_list, fmt=fmt, indent=json_indent)


@cli.command("viewer")
@babel_options
@nodenorm_options
@nameres_options
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=click.IntRange(0, 65535))
@click.option(
    "--open-browser/--no-open-browser",
    default=True,
    show_default=True,
    help="Open the viewer in the default browser after the server starts",
)
def viewer_command(
    babel_url: str | None,
    babel_releases_url: str,
    babel_version: str,
    nodenorm_url: str,
    nameres_url: str,
    local_dir: str,
    check_download: str,
    allow_version_mismatch: bool,
    host: str,
    port: int,
    open_browser: bool,
):
    """Run the local NodeNorm clique and Babel concordance graph viewer."""
    downloader = make_downloader(
        babel_url, babel_releases_url, babel_version, local_dir, check_download
    )
    nodenorm = NodeNorm(nodenorm_url)
    check_babel_versions(downloader, nodenorm, allow_version_mismatch)
    service = ViewerService(
        BabelXRefs(downloader, nodenorm),
        NameResolver(nameres_url),
        downloader.babel_version,
    )
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    click.echo(f"Serving Babel Explorer viewer at http://{browser_host}:{port}/")
    try:
        serve_viewer(service, host=host, port=port, open_browser=open_browser)
    except OSError as error:
        raise click.ClickException(f"Could not start viewer: {error}") from error


@cli.command("ids")
@click.argument("curies", type=str, required=True, nargs=-1)
@babel_options
@nodenorm_options
@click.option("--labels", is_flag=True, help="Include labels for CURIEs")
@format_option
def ids(
    curies: list[str],
    babel_url: str | None,
    babel_releases_url: str,
    babel_version: str,
    nodenorm_url: str,
    local_dir: str,
    labels: bool,
    check_download: str,
    allow_version_mismatch: bool,
    fmt: str,
    json_indent: int,
):
    """
    Fetches and prints the ID records for the given CURIEs, along with Biolink type if provided.

    \f

    :param curies: A list of CURIEs (Compact URI) for which cross-references need
        to be retrieved.
    :type curies: list[str]
    :param babel_url: Complete URL of one Babel release, overriding the two below.
        ``None`` unless ``--babel-url`` was passed.
    :type babel_url: str | None
    :param babel_releases_url: URL of a directory holding one subdirectory per release.
    :type babel_releases_url: str
    :param babel_version: Which release subdirectory to query, or ``latest``.
    :type babel_version: str

    :return: None
    """
    downloader = make_downloader(
        babel_url, babel_releases_url, babel_version, local_dir, check_download
    )
    nodenorm = NodeNorm(nodenorm_url)
    # NodeNorm is only consulted for labels, so only then can its Babel release differ.
    if labels:
        check_babel_versions(downloader, nodenorm, allow_version_mismatch)

    bxref = BabelXRefs(downloader, nodenorm)
    xrefs = bxref.get_curie_ids(curies, label_curies=labels)

    if fmt == "console":
        console = make_console()
        for record in xrefs:
            console.print(format_identifier_record(record))
    else:
        write_records(xrefs, fmt=fmt, indent=json_indent)


@cli.command("test-concord")
@click.argument("curies", type=str, required=True, nargs=-1)
@nodenorm_options
@format_option
def test_concord(curies, nodenorm_url, fmt, json_indent):
    """For each CURIE, print the current NodeNorm clique (all equivalent identifiers, labels, and Biolink types).

    Useful for inspecting how a potential Babel concordance change would affect NodeNorm:
    run before and after a Babel rebuild to see how cliques would shift.
    """
    nodenorm = NodeNorm(nodenorm_url)
    nodenorm.normalize_curies(curies)

    # Resolved once, before the format branch, so console and JSON report the same rows.
    query_set = set(curies)
    cliques = [
        (curie, ident)
        for curie in curies
        for ident in nodenorm.get_clique_identifiers(curie)
    ]

    if fmt == "console":
        console = make_console()
        for curie, ident in cliques:
            member = curie_with_label(
                ident.curie, _depth_of(ident.curie, query_set, None), ident.label
            )
            biolink = escape(", ".join(ident.biolink_type))
            console.print(f"{hl_curie(curie, 0)}  {member}  [dim]{biolink}[/dim]")
    else:
        write_records(
            [
                {"query_curie": curie, **record_to_dict(ident)}
                for curie, ident in cliques
            ],
            fmt=fmt,
            indent=json_indent,
        )


if __name__ == "__main__":
    cli()
