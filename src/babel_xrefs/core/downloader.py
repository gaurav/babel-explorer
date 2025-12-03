import os
import urllib.parse
import subprocess
import requests
import logging

class BabelDownloader:
    """
    Class for downloading Babel cross-reference files to a local directory as needed.
    """

    def __init__(self, url_base, local_path=None, retries=10):
        # We assume the URL base is correct (if not, we can fix it later).
        self.url_base = url_base
        self.retries = retries
        self.logger = logging.getLogger(BabelDownloader.__name__)

        if local_path is None:
            # Default to using TMPDIR.
            # TODO: replace with a real temporary directory.
            tmpdir = os.environ.get("TMPDIR")
            if tmpdir:
                local_path = tmpdir

        # Make sure the local path is an existing directory or that we can create it.
        if not os.path.exists(local_path):
            os.makedirs(local_path, exist_ok=True)
            self.local_path = local_path
        elif os.path.exists(local_path) and os.path.isdir(local_path):
            self.local_path = local_path
        else:
            raise ValueError(f"Invalid local_path (must be an existing directory): '{local_path}'")

    def get_output_file(self, filename):
        filepath = os.path.join(self.local_path, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        return filepath

    def get_downloaded_file(self, dirpath: str, chunk_size:int=1024*1024):
        local_path_to_download_to = os.path.join(self.local_path, dirpath)
        os.makedirs(os.path.dirname(local_path_to_download_to), exist_ok=True)

        url_to_download = urllib.parse.urljoin(self.url_base, dirpath)
        bytes_downloaded = 0

        wget_command_line = [
            "wget",
            "--progress=bar:force:noscroll",        # Display progress bar.
            "--compression=auto",                   # Compress files if available.
            "--continue",                           # Continue downloading in case of interruption.
            f"--tries={self.retries}",
            "-O" + local_path_to_download_to,
        ]

        # Add URL and output file.
        wget_command_line.append(url_to_download)

        # Execute wget.
        self.logger.info(f"Downloading {url_to_download} using wget: {wget_command_line}")
        process = subprocess.run(wget_command_line)
        if process.returncode != 0:
            raise RuntimeError(f"Could not execute wget {wget_command_line}: {process.stderr}")

        bytes_downloaded = os.path.getsize(local_path_to_download_to)
        self.logger.info(f"Downloaded {url_to_download} to {local_path_to_download_to}: {bytes_downloaded} bytes")
        return local_path_to_download_to


    def get_downloaded_dir(self, dirpath: str):
        local_path_to_download_to = os.path.join(self.local_path, dirpath)
        os.makedirs(os.path.dirname(local_path_to_download_to), exist_ok=True)

        url_to_download_recursively = urllib.parse.urljoin(self.url_base, dirpath)

        wget_command_line = [
            "wget",
            "--progress=bar:force:noscroll",        # Display progress bar.
            "--compression=auto",                   # Compress files if available.
            "--continue",                           # Continue downloading in case of interruption.
            f"--tries={self.retries}",
            "--recursive",
            "--no-parent",
            "--no-host-directories",
            "--directory-prefix=" + local_path_to_download_to,
        ]

        # Add URL and output file.
        if url_to_download_recursively[-1] != "/":
            url_to_download_recursively += "/"
        wget_command_line.append(url_to_download_recursively)

        # Execute wget.
        self.logger.info(f"Downloading {url_to_download_recursively} using wget: {wget_command_line}")
        process = subprocess.run(wget_command_line)
        if process.returncode != 0:
            raise RuntimeError(f"Could not execute wget {wget_command_line}: {process.stderr}")

        return local_path_to_download_to
