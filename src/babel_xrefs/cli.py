# Command line interface for babel-xrefs
import click
import logging
from babel_xrefs.core.downloader import BabelDownloader
from babel_xrefs.babel_xrefs import BabelXRefs
from babel_xrefs.core.nodenorm import NodeNorm


@click.group()
def cli():
    pass

@cli.command("xrefs")
@click.argument("curies", type=str, required=True, nargs=-1)
@click.option("--local-dir", type=str, default="data/2025nov19", help="Local location to save Babel download files to")
@click.option("--babel-url", type=str, default="https://stars.renci.org:443/var/babel_outputs/2025nov19/", help="Base URL of the Babel server")
@click.option("--nodenorm-url", type=str, default="https://nodenormalization-sri.renci.org/", help="NodeNorm URL to check for concord changes")
@click.option("--expand", is_flag=True, help="Also display xrefs for returned CURIEs")
@click.option("--labels", is_flag=True, help="Include labels for CURIEs")
def xrefs(curies: list[str], babel_url: str, nodenorm_url, local_dir: str, expand: bool, labels: bool):
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

    bxref = BabelXRefs(BabelDownloader(babel_url, local_path=local_dir), NodeNorm(nodenorm_url))
    xrefs = bxref.get_curie_xrefs(curies, expand, label_curies=labels)
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

@cli.command("test-concord")
@click.argument("curies", type=str, required=True, nargs=-1)
@click.option("--nodenorm-url", type=str, default="https://nodenormalization-sri.renci.org/", help="NodeNorm URL to check for concord changes")
def test_concord(curies, nodenorm_url):
    # We're trying to answer a simple question here: if the CURIEs we mention were combined, how would the cliques change in NodeNorm?
    # By definition, this can only combine all the cliques mentioned in the CURIEs.

    nodenorm = NodeNorm(nodenorm_url)
    for curie in curies:
        identifiers = nodenorm.get_clique_identifiers(curie)
        for identifier in identifiers:
            if identifier.label:
                print(f"{curie}\t{identifier.curie}\t{identifier.label}\t{identifier.biolink_type}")
            else:
                print(f"{curie}\t{identifier.curie}\t\t{identifier.biolink_type}")


if __name__ == "__main__":
    cli()
