"""Client for the Translator Name Resolver lookup API."""

import dataclasses

import requests


@dataclasses.dataclass(frozen=True)
class NameResolution:
    """One concept returned by Name Resolver."""

    curie: str
    label: str
    types: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    score: float = 0.0
    clique_identifier_count: int = 0

    @staticmethod
    def from_dict(value: dict) -> "NameResolution":
        return NameResolution(
            curie=value["curie"],
            label=value["label"],
            types=tuple(value.get("types") or ()),
            synonyms=tuple(value.get("synonyms") or ()),
            score=float(value.get("score") or 0),
            clique_identifier_count=int(value.get("clique_identifier_count") or 0),
        )


class NameResolver:
    """Small client for Name Resolver's autocomplete endpoint."""

    def __init__(
        self,
        url: str = "https://name-resolution-sri.renci.org/",
        timeout: int = 30,
    ):
        self.url = url.rstrip("/") + "/"
        self.timeout = timeout

    def lookup(self, query: str, limit: int = 12) -> list[NameResolution]:
        """Return autocomplete matches for *query*, ordered by Name Resolver."""
        query = query.strip()
        if not query:
            return []
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        response = requests.get(
            f"{self.url}lookup",
            params={
                "string": query,
                "autocomplete": True,
                "highlighting": False,
                "limit": limit,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Name Resolver returned an unexpected response")
        return [NameResolution.from_dict(result) for result in payload]
