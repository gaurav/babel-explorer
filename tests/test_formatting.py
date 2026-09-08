"""
Unit tests for formatting.py — no network, no mocking required.
"""

import io
import json

import pytest
from rich.console import Console

from babel_explorer.core.babel_xrefs import (
    CrossReference,
    IdentifierRecord,
    LabeledCrossReference,
)
from babel_explorer.core.nodenorm import Identifier
from babel_explorer.formatting import (
    curie_with_label,
    escape_label,
    format_identifier_record,
    hl_curie,
    make_console,
    record_to_dict,
    write_records,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def xref():
    return CrossReference(
        filename="Concord.parquet", subj="A:1", pred="skos:exactMatch", obj="B:2"
    )


@pytest.fixture
def labeled_xref():
    return LabeledCrossReference(
        filename="Concord.parquet",
        subj="A:1",
        pred="skos:exactMatch",
        obj="B:2",
        subj_label="Alpha",
        subj_biolink_type=("biolink:Disease",),
        obj_label="Beta",
        obj_biolink_type=("biolink:Gene", "biolink:NamedThing"),
    )


@pytest.fixture
def id_record():
    return IdentifierRecord(
        curie="A:1",
        extra_fields=(("type", "gene"), ("label", "Alpha")),
    )


@pytest.fixture
def identifier():
    return Identifier(
        curie="MONDO:0004979",
        label="asthma",
        biolink_type=("biolink:Disease",),
        taxa=("NCBITaxon:9606",),
        description=("A chronic inflammatory disease",),
    )


# ---------------------------------------------------------------------------
# Tests for make_console and hl_curie
# ---------------------------------------------------------------------------


class TestConsoleUtilities:
    def test_make_console_returns_console(self):
        console = make_console()
        assert isinstance(console, Console)

    def test_make_console_accepts_file(self):
        out = io.StringIO()
        console = make_console(file=out)
        assert isinstance(console, Console)
        console.print("hello")
        assert "hello" in out.getvalue()

    def test_hl_curie_highlighted_contains_markup(self):
        result = hl_curie("HGNC:1100", 0)
        assert "bold cyan" in result
        assert "HGNC:1100" in result

    def test_hl_curie_not_highlighted_is_plain(self):
        result = hl_curie("HGNC:1100", None)
        assert result == "HGNC:1100"
        assert "[" not in result

    def test_hl_curie_highlighted_renders_correctly(self):
        """Markup renders to plain text on a non-TTY console."""
        out = io.StringIO()
        console = Console(file=out, highlight=False, no_color=True)
        console.print(hl_curie("HGNC:1100", 0))
        assert "HGNC:1100" in out.getvalue()

    def test_hl_curie_highlighted_renders_with_color(self):
        """On a forced-TTY console, ANSI codes are emitted."""
        out = io.StringIO()
        console = Console(file=out, highlight=False, force_terminal=True)
        console.print(hl_curie("HGNC:1100", 0))
        output = out.getvalue()
        assert "HGNC:1100" in output
        assert "\x1b[" in output  # ANSI escape present


# ---------------------------------------------------------------------------
# Tests for record_to_dict
# ---------------------------------------------------------------------------


class TestRecordToDict:
    def test_cross_reference(self, xref):
        d = record_to_dict(xref)
        assert d == {
            "filename": "Concord.parquet",
            "subj": "A:1",
            "pred": "skos:exactMatch",
            "obj": "B:2",
        }

    def test_labeled_cross_reference_has_all_eight_fields(self, labeled_xref):
        d = record_to_dict(labeled_xref)
        assert set(d.keys()) == {
            "filename",
            "subj",
            "pred",
            "obj",
            "subj_label",
            "subj_biolink_type",
            "obj_label",
            "obj_biolink_type",
        }
        # dataclasses.asdict() preserves tuple types
        assert d["subj_biolink_type"] == ("biolink:Disease",)
        assert d["obj_biolink_type"] == ("biolink:Gene", "biolink:NamedThing")

    def test_identifier_record_extra_fields_expanded(self, id_record):
        d = record_to_dict(id_record)
        assert "extra_fields" not in d
        assert d["curie"] == "A:1"
        assert d["type"] == "gene"
        assert d["label"] == "Alpha"

    def test_identifier_record_no_extra_fields(self):
        rec = IdentifierRecord(curie="X:1")
        d = record_to_dict(rec)
        assert d == {"curie": "X:1"}

    def test_plain_dict_passthrough(self):
        data = {"a": 1, "b": "hello"}
        assert record_to_dict(data) is data

    def test_identifier_dataclass(self, identifier):
        d = record_to_dict(identifier)
        assert d["curie"] == "MONDO:0004979"
        assert d["label"] == "asthma"
        # dataclasses.asdict() preserves tuple types
        assert d["biolink_type"] == ("biolink:Disease",)
        assert d["taxa"] == ("NCBITaxon:9606",)


# ---------------------------------------------------------------------------
# Tests for write_records
# ---------------------------------------------------------------------------


class TestWriteRecords:
    # -- json format --

    def test_json_is_valid_list(self, xref):
        out = io.StringIO()
        write_records([xref], "json", file=out)
        data = json.loads(out.getvalue())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["subj"] == "A:1"

    def test_json_empty_list(self):
        out = io.StringIO()
        write_records([], "json", file=out)
        assert json.loads(out.getvalue()) == []

    def test_json_indent_controls_formatting(self, xref):
        out_pretty = io.StringIO()
        write_records([xref], "json", indent=2, file=out_pretty)

        out_compact = io.StringIO()
        write_records([xref], "json", indent=None, file=out_compact)

        # Pretty-printed output has more lines (has newlines per field)
        assert out_pretty.getvalue().count("\n") > out_compact.getvalue().count("\n")

    def test_json_tuple_fields_serialized_as_arrays(self, labeled_xref):
        # json.dump converts tuples to JSON arrays, so json.loads gives back lists
        out = io.StringIO()
        write_records([labeled_xref], "json", file=out)
        data = json.loads(out.getvalue())
        assert isinstance(data[0]["subj_biolink_type"], list)
        assert data[0]["obj_biolink_type"] == ["biolink:Gene", "biolink:NamedThing"]

    def test_json_plain_dict(self):
        out = io.StringIO()
        write_records([{"a": 1, "b": "x"}], "json", file=out)
        assert json.loads(out.getvalue()) == [{"a": 1, "b": "x"}]

    # -- tsv format --

    def test_tsv_has_header_row(self, xref):
        out = io.StringIO()
        write_records([xref], "tsv", file=out)
        lines = out.getvalue().splitlines()
        assert lines[0] == "filename\tsubj\tpred\tobj"

    def test_tsv_data_row(self, xref):
        out = io.StringIO()
        write_records([xref], "tsv", file=out)
        lines = out.getvalue().splitlines()
        assert lines[1] == "Concord.parquet\tA:1\tskos:exactMatch\tB:2"

    def test_tsv_tuple_fields_pipe_joined(self, labeled_xref):
        out = io.StringIO()
        write_records([labeled_xref], "tsv", file=out)
        lines = out.getvalue().splitlines()
        # Header row
        assert "subj_biolink_type" in lines[0]
        # Data row: multi-value tuple joined with pipe
        assert "biolink:Gene|biolink:NamedThing" in lines[1]

    def test_tsv_empty_no_output(self):
        out = io.StringIO()
        write_records([], "tsv", file=out)
        assert out.getvalue() == ""

    def test_tsv_identifier_record_extra_fields_expanded(self, id_record):
        out = io.StringIO()
        write_records([id_record], "tsv", file=out)
        lines = out.getvalue().splitlines()
        assert "curie" in lines[0]
        assert "type" in lines[0]
        assert "label" in lines[0]
        assert "A:1" in lines[1]

    # -- csv format --

    def test_csv_has_header_row(self, xref):
        out = io.StringIO()
        write_records([xref], "csv", file=out)
        lines = out.getvalue().splitlines()
        assert lines[0] == "filename,subj,pred,obj"

    def test_csv_data_row(self, xref):
        out = io.StringIO()
        write_records([xref], "csv", file=out)
        lines = out.getvalue().splitlines()
        assert lines[1] == "Concord.parquet,A:1,skos:exactMatch,B:2"

    def test_csv_empty_no_output(self):
        out = io.StringIO()
        write_records([], "csv", file=out)
        assert out.getvalue() == ""

    def test_csv_tuple_fields_pipe_joined(self, labeled_xref):
        out = io.StringIO()
        write_records([labeled_xref], "csv", file=out)
        lines = out.getvalue().splitlines()
        assert "biolink:Gene|biolink:NamedThing" in lines[1]

    # -- invalid formats (including console, which is handled at CLI layer) --

    def test_text_format_raises_value_error(self, xref):
        out = io.StringIO()
        with pytest.raises(ValueError, match="Unknown format"):
            write_records([xref], "text", file=out)

    def test_console_format_raises_value_error(self, xref):
        """Console format is handled by the CLI, not write_records."""
        out = io.StringIO()
        with pytest.raises(ValueError, match="Unknown format"):
            write_records([xref], "console", file=out)

    def test_unknown_format_raises_value_error(self, xref):
        out = io.StringIO()
        with pytest.raises(ValueError, match="Unknown format"):
            write_records([xref], "xml", file=out)


# ---------------------------------------------------------------------------
# Tests for the label convention (escape_label / curie_with_label)
# ---------------------------------------------------------------------------


class TestLabelConvention:
    """AGENTS.md: a label follows its CURIE in double quotes, or is omitted."""

    def test_label_follows_curie_in_double_quotes(self):
        assert curie_with_label("MONDO:1", None, "asthma") == 'MONDO:1 "asthma"'

    @pytest.mark.parametrize("label", [None, ""])
    def test_absent_label_is_omitted_entirely(self, label):
        """No placeholder — a CURIE with no label renders as the bare CURIE."""
        assert curie_with_label("MONDO:1", None, label) == "MONDO:1"

    def test_backslashes_escape_before_quotes(self):
        assert escape_label(r'a\b"c') == r"a\\b\"c"

    def test_escaped_label_matches_the_documented_regex(self):
        import re

        rendered = curie_with_label("MONDO:1", None, r'say "hi" \ bye')
        assert re.search(r'"([^"\\]|\\.)*"', rendered).group(0) == (
            r'"say \"hi\" \\ bye"'
        )

    def test_query_curie_is_highlighted_at_depth_zero(self):
        assert curie_with_label("MONDO:1", 0, "asthma").startswith("[bold cyan]")

    def test_identifier_record_uses_the_same_convention(self):
        rec = IdentifierRecord(
            curie="A:1", extra_fields=(("n", 1),), nodenorm_label='a"b'
        )
        rendered = format_identifier_record(rec)
        assert r'nodenorm_label="a\"b"' in rendered

    def test_identifier_record_omits_absent_label(self):
        rec = IdentifierRecord(curie="A:1", extra_fields=(("n", 1),))
        assert "label=" not in format_identifier_record(rec)

    def test_nodenorm_label_does_not_collide_with_the_parquet_label_column(self):
        """Identifiers.parquet carries its own label column; both must survive."""
        rec = IdentifierRecord(
            curie="MONDO:0004979",
            extra_fields=(("label", "asthma (Babel)"),),
            nodenorm_label="asthma (NodeNorm)",
        )
        d = record_to_dict(rec)
        assert d["nodenorm_label"] == "asthma (NodeNorm)"
        assert d["label"] == "asthma (Babel)"


class TestLabelOmissionInRecords:
    """The omit-when-absent rule covers the NodeNorm label fields, and only those."""

    def test_an_empty_parquet_label_column_is_kept(self):
        """Identifiers.parquet's own `label` column is data, not an absent lookup.

        Dropping it when empty would give some rows of a json/tsv/csv run a `label`
        key and others none, so `row["label"]` raises KeyError downstream.
        """
        rec = IdentifierRecord(curie="A:1", extra_fields=(("label", ""),))
        d = record_to_dict(rec)
        assert d["label"] == ""
        assert "nodenorm_label" not in d

    def test_json_rows_all_carry_the_parquet_label_column(self):
        out = io.StringIO()
        write_records(
            [
                IdentifierRecord(curie="A:1", extra_fields=(("label", ""),)),
                IdentifierRecord(curie="B:2", extra_fields=(("label", "asthma"),)),
            ],
            "json",
            file=out,
        )
        rows = json.loads(out.getvalue())
        assert [r["label"] for r in rows] == ["", "asthma"]

    def test_empty_subj_and_obj_labels_are_dropped(self):
        xref = LabeledCrossReference(
            filename="f",
            subj="A:1",
            pred="p",
            obj="B:2",
            subj_label="",
            subj_biolink_type=(),
            obj_label="",
            obj_biolink_type=(),
        )
        d = record_to_dict(xref)
        assert "subj_label" not in d and "obj_label" not in d

    def test_present_labels_are_kept(self):
        xref = LabeledCrossReference(
            filename="f",
            subj="A:1",
            pred="p",
            obj="B:2",
            subj_label="asthma",
            subj_biolink_type=(),
            obj_label="",
            obj_biolink_type=(),
        )
        d = record_to_dict(xref)
        assert d["subj_label"] == "asthma" and "obj_label" not in d

    def test_tabular_output_survives_rows_with_differing_keys(self):
        """A labelled row after an unlabelled one must not blow up DictWriter."""
        rows = [
            IdentifierRecord(curie="A:1", extra_fields=()),
            IdentifierRecord(curie="B:2", extra_fields=(), nodenorm_label="asthma"),
        ]
        out = io.StringIO()
        write_records(rows, "csv", file=out)
        lines = out.getvalue().splitlines()
        assert lines[0] == "curie,nodenorm_label"
        assert lines[1] == "A:1,"
        assert lines[2] == "B:2,asthma"
