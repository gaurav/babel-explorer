"""Unit tests for the Name Resolver client."""

from unittest.mock import Mock, patch

import pytest

from babel_explorer.core.nameres import NameResolver


def test_lookup_parses_name_resolver_results():
    response = Mock()
    response.json.return_value = [
        {
            "curie": "MONDO:0004979",
            "label": "asthma",
            "types": ["biolink:Disease"],
            "synonyms": ["Asthma"],
            "score": 3600.077,
            "clique_identifier_count": 28,
        }
    ]
    response.raise_for_status.return_value = None

    with patch(
        "babel_explorer.core.nameres.requests.get", return_value=response
    ) as get:
        results = NameResolver("https://nameres.example").lookup("asthma", limit=5)

    assert results[0].curie == "MONDO:0004979"
    assert results[0].types == ("biolink:Disease",)
    assert results[0].clique_identifier_count == 28
    get.assert_called_once_with(
        "https://nameres.example/lookup",
        params={
            "string": "asthma",
            "autocomplete": True,
            "highlighting": False,
            "limit": 5,
        },
        timeout=30,
    )


def test_lookup_empty_query_does_not_call_api():
    with patch("babel_explorer.core.nameres.requests.get") as get:
        assert NameResolver().lookup("  ") == []
    get.assert_not_called()


def test_lookup_rejects_invalid_limit():
    with pytest.raises(ValueError, match="between 1 and 1000"):
        NameResolver().lookup("asthma", limit=0)


def test_lookup_rejects_non_list_response():
    response = Mock()
    response.json.return_value = {"unexpected": "shape"}
    response.raise_for_status.return_value = None

    with (
        patch("babel_explorer.core.nameres.requests.get", return_value=response),
        pytest.raises(ValueError, match="unexpected response"),
    ):
        NameResolver().lookup("asthma")
