# Command line interface for babel-xrefs
import click
import logging
from babel_xrefs.core.downloader import BabelDownloader
from babel_xrefs.babel_xrefs import BabelXRefs


@click.group()
def cli():
    pass

@cli.command("xrefs")
@click.argument("curies", type=str, required=True, nargs=-1)
@click.option("--local-dir", type=str, default="data/2025nov19", help="Local location to save Babel download files to")
@click.option("--babel-url", type=str, default="https://stars.renci.org:443/var/babel_outputs/2025nov19/", help="Base URL of the Babel server")
@click.option("--expand", is_flag=True, help="Also display xrefs for returned CURIEs")
def xrefs(curies: list[str], babel_url: str, local_dir: str, expand: bool):
    """
    Fetches and prints the cross-references (xrefs) for the given CURIEs.

    This function searches for xrefs associated with the provided CURIEs.

    \f

    :param curies: A list of CURIEs (Compact URI) for which cross-references need
        to be retrieved.
    :type curies: list[str]
    :param babel_url: Base URL of the Babel server
    :type babel_url: str

    :return: None
    """
    logging.basicConfig(level=logging.INFO)

    bxref = BabelXRefs(BabelDownloader(babel_url, local_path=local_dir))
    xrefs = bxref.get_curie_xrefs(curies, expand)
    for xref in xrefs:
        print(xref)

@cli.command("ids")
@click.argument("curies", type=str, required=True, nargs=-1)
@click.option("--local-dir", type=str, default="data/2025nov19", help="Local location to save Babel download files to")
@click.option("--babel-url", type=str, default="https://stars.renci.org:443/var/babel_outputs/2025nov19/", help="Base URL of the Babel server")
def ids(curies: list[str], babel_url: str, local_dir: str):
    """
    Fetches and prints the ID records for the given CURIEs, along with Biolink type if provided.

    \f

    :param curies: A list of CURIEs (Compact URI) for which cross-references need
        to be retrieved.
    :type curies: list[str]
    :param babel_url: Base URL of the Babel server
    :type babel_url: str

    :return: None
    """
    logging.basicConfig(level=logging.INFO)

    bxref = BabelXRefs(BabelDownloader(babel_url, local_path=local_dir))
    xrefs = bxref.get_curie_ids(curies)
    for xref in xrefs:
        print(xref)

if __name__ == "__main__":
    cli()
