# Babel XRefs is a tool for accessing and querying the intermediate files
# that we make available with Babel builds. This allows you to find out
# why we consider two identifiers to be identical.
import dataclasses
import logging
import duckdb
import functools

from babel_xrefs.core.downloader import BabelDownloader

@dataclasses.dataclass(frozen=True)
class CrossReference:
    filename: str
    subj: str
    pred: str
    obj: str

    @staticmethod
    def from_tuple(tuple: tuple[str, str, str, str]):
        return CrossReference(filename=tuple[0], subj=tuple[1], pred=tuple[2], obj=tuple[3])

    @property
    def curies(self):
        return frozenset([self.subj, self.obj])

    def __lt__(self, other):
        return (self.filename, self.subj, self.obj, self.pred) < (other.filename, other.subj, other.obj, other.pred)

class BabelXRefs:
    def __init__(self, downloader: BabelDownloader):
        self.downloader = downloader

    def get_curie_ids(self, curies: list[str]):
        """
        Search for all identifiers in the /ids/ files for a particular CURIE.

        :param curie: A CURIE to search for.
        :return: A list of cross-references containing that CURIE.
        """

        identifier_parquet = self.downloader.get_downloaded_file('duckdb/Identifiers.parquet')
        concord_metadata_parquet = self.downloader.get_downloaded_file('duckdb/Metadata.parquet')

        # Query the Parquet files using DuckDB.
        duckdb_path = self.downloader.get_output_file('output/duckdbs/xrefs.duckdb')
        db = duckdb.connect(duckdb_path)
        identifier_table = db.read_parquet(identifier_parquet)
        xrefs = db.execute(f"SELECT * FROM identifier_table WHERE curie IN $1", [curies])

        # TODO: convert into case classes.

        return xrefs.fetchall()

    @functools.lru_cache(maxsize=None)
    def get_curie_xref(self, curie: str):
        concord_parquet = self.downloader.get_downloaded_file('duckdb/Concord.parquet')
        concord_metadata_parquet = self.downloader.get_downloaded_file('duckdb/Metadata.parquet')

        duckdb_path = self.downloader.get_output_file('output/duckdbs/xrefs.duckdb')
        db = duckdb.connect(duckdb_path)
        concord_table = db.read_parquet(concord_parquet)
        xref_tuples = db.execute(f"SELECT filename, subj, pred, obj FROM concord_table WHERE subj=$1 OR obj=$1", [curie]).fetchall()
        xrefs = list(map(lambda rec: CrossReference.from_tuple(rec), xref_tuples))
        return xrefs

    def get_curie_xrefs(self, curies: list[str], expand: bool = False, ignore_curies_in_expansion: set = set()):
        """
        Search for all identifiers that are cross-referenced to the given CURIE.

        :param curie: A CURIE to search for.
        :param expand: Whether to expand the cross-references (i.e. recursively follow all identifiers).
        :return: A list of cross-references containing that CURIE.
        """

        xrefs = set()
        for curie in curies:
            logging.info(f"Searching for cross-references for {curie}")
            xrefs.update(self.get_curie_xref(curie))

        if expand:
            # Get a unique set of referenced curies, not including the ones currently queried.
            new_curies = list(set([curie for xref in xrefs for curie in xref.curies]) - set(curies) - ignore_curies_in_expansion)
            if new_curies:
                logging.info(f"Expanding cross-references to {new_curies}")
                xrefs.update(self.get_curie_xrefs(new_curies, expand=True, ignore_curies_in_expansion=ignore_curies_in_expansion | set(new_curies)))

        return sorted(xrefs)
