import pytest

from app.csv_parser import CsvFormatError, parse_csv


def parse(text: str, max_rows: int = 20):
    return parse_csv(text.encode(), max_rows=max_rows)


def test_parses_with_header_and_optional_phone():
    report = parse("name,address,phone\nA,1 St,555-1\nB,2 St,\n")
    assert report.valid
    assert [(h.row, h.name, h.phone) for h in report.hospitals] == [(1, "A", "555-1"), (2, "B", None)]


def test_header_is_optional_and_two_column_rows_ok():
    report = parse("A,1 St\nB,2 St,555 1234\n")
    assert report.valid and report.total_rows == 2


def test_two_column_header_accepted():
    assert parse("name,address\nA,1 St\n").valid


def test_quoted_fields_with_commas():
    report = parse('name,address,phone\n"St. Mary\'s, North","12 Main St, Apt 4",+1 (555) 010-2000\n')
    assert report.valid
    h = report.hospitals[0]
    assert h.name == "St. Mary's, North" and h.address == "12 Main St, Apt 4"


def test_utf8_bom_and_blank_lines_are_tolerated():
    report = parse_csv("﻿name,address\n\nA,1 St\n\n".encode("utf-8"), max_rows=20)
    assert report.valid and report.total_rows == 1


@pytest.mark.parametrize(
    "line,field",
    [(",1 St", "name"), ("A,", "address"), ("A,1 St,not-a-phone!", "phone")],
)
def test_row_level_errors_are_reported_with_field(line, field):
    report = parse(f"name,address,phone\nOK,1 St\n{line}\n")
    assert not report.valid
    assert [(e.row, e.field) for e in report.errors] == [(2, field)]


def test_wrong_column_counts():
    report = parse("A\nB,2,3,4\n")
    assert [e.row for e in report.errors] == [1, 2]


def test_duplicate_rows_flagged():
    report = parse("A,1 St\na,1 st\n")
    assert not report.valid and "Duplicate of row 1" in report.errors[0].message


def test_all_errors_collected_not_just_first():
    report = parse(",,555-1\n,,555-2\n,,555-3\n")
    assert len(report.errors) == 6  # name + address on each of 3 rows


def test_max_rows_enforced():
    body = "\n".join(f"H{i},{i} St" for i in range(21))
    with pytest.raises(CsvFormatError, match="maximum per upload is 20"):
        parse(body)


@pytest.mark.parametrize("raw", [b"", b"   \n", b"name,address,phone\n"])
def test_empty_inputs_rejected(raw):
    with pytest.raises(CsvFormatError):
        parse_csv(raw, max_rows=20)


def test_non_utf8_rejected():
    with pytest.raises(CsvFormatError, match="UTF-8"):
        parse_csv(b"\xff\xfe\x00A", max_rows=20)


def test_unexpected_header_rejected():
    with pytest.raises(CsvFormatError, match="Unexpected header"):
        parse("name,address,email\nA,1 St,a@b.c\n")
