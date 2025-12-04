# Babel XRefs is a tool for accessing and querying the intermediate files
# that we make available with Babel builds. This allows you to find out
# why we consider two identifiers to be identical.
import logging
import duckdb

from babel_xrefs.core.downloader import BabelDownloader


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


    def get_curie_xrefs(self, curies: list[str]):
        """
        Search for all identifiers that are cross-referenced to the given CURIE.

        :param curie: A CURIE to search for.
        :return: A list of cross-references containing that CURIE.
        """

        concord_parquet = self.downloader.get_downloaded_file('duckdb/Concord.parquet')
        concord_metadata_parquet = self.downloader.get_downloaded_file('duckdb/Metadata.parquet')

        # Query the Parquet files using DuckDB.
        duckdb_path = self.downloader.get_output_file('output/duckdbs/xrefs.duckdb')
        db = duckdb.connect(duckdb_path)
        concord_table = db.read_parquet(concord_parquet)
        xrefs = db.execute(f"SELECT * FROM concord_table WHERE subj IN $1 OR obj in $1", [curies])

        # TODO: convert into case classes.

        return xrefs.fetchall()
