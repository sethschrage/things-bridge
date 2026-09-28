"""tb1 frames, field conversion, row -> model conversion, argv encoding and osascript error parsing
(§2.5, §4.1, §4.4). Pure: nothing here runs osascript."""

import dataclasses
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from things_bridge import applescript as a
from things_bridge.applescript import RS, US, Frame, parse_frame
from things_bridge.errors import InvalidInput, ProtocolError
from things_bridge.models import NOTES_PREVIEW_CHARS, Area, Item, Ref, Tag, link_for

FIXTURES = Path(__file__).parent / "fixtures" / "tb1"
TODO, PROJ, AREA = "SyJEz273ceSkabUbciM73A", "Jvj7EW1fLoScPhaw2JomCT", "Lg8UqVPXo2SbJNiBpDBBQ"
NOTES = 'Low fat.\r\nFrom the "good" shop – ünïcødé 🥛\r\n\ttabbed'


def frame(*records: list[str], newline: bool = True) -> bytes:
    """Raw osascript stdout for ``records`` (osascript appends one newline)."""
    return (RS.join(US.join(r) for r in records) + ("\n" if newline else "")).encode("utf-8")


def scan(*rows: list[str], count: str | None = None, **kw) -> bytes:
    return frame(["H", "tb1", "scan", "list", "today", "7200"], *rows,
                 ["E", str(len(rows)) if count is None else count], **kw)


def s_row(id: str = TODO, name: str = "Buy milk", status: str = "open", tags: str = "", due: str = "",
          notes: str = "") -> list[str]:
    return ["S", id, name, status, tags, due, notes]


def t_row(id: str = TODO, **fields: str) -> list[str]:
    row = dict.fromkeys(a.ROW_T, "")
    row.update(tag="T", id=id, kind="to-do", name="Buy milk", status="open")
    row.update(fields)
    return [row[k] for k in a.ROW_T]


def rows_frame(*rows: list[str]) -> Frame:
    primary = sum(r[0] not in ("R", "D") for r in rows)
    return parse_frame(frame(["H", "tb1", "rows", "7200"], *rows, ["E", str(primary)]), "ROWS")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def tz():
    """Switch the process time zone (``TZ`` + ``time.tzset``) for one test, then restore it."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is Unix-only")
    old = os.environ.get("TZ")

    def use(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield use
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def berlin(tz):
    tz("Europe/Berlin")
    if time.localtime(1_768_478_400).tm_gmtoff != 3600:          # 2026-01-15: needs tzdata
        pytest.skip("Europe/Berlin zone data not available")


# ---- frames -------------------------------------------------------------------------------------

def test_happy_path_scan():
    f = parse_frame(fixture("scan_list_today.bin"), "SCAN")
    assert f.template == "SCAN"
    assert f.header == ["H", "tb1", "scan", "list", "today", "7200"]
    assert [r[0] for r in f.rows] == ["S", "S", "S"]
    assert f.of("S")[0] == ["S", TODO, "Buy milk 🥛", "open", "Errand, Home", "2026-10-02", NOTES]
    assert f.of("S")[2][2] == "  Pay rent  "                      # no stripping of fields
    assert f.of("S")[2][6] == "line1\nline2\n"                    # newline inside the frame survives
    assert f.of("T") == []


def test_frame_of_keeps_output_order():
    f = parse_frame(fixture("rows_mixed.bin"), "ROWS")
    assert [r[0] for r in f.rows] == ["T", "T", "A", "X", "R", "R"]
    assert [r[1] for r in f.of("T")] == [TODO, PROJ]
    assert [r[2] for r in f.of("R")] == [PROJ, AREA]


FIXTURE_TEMPLATES = {
    "scan_list_today.bin": "SCAN", "rows_mixed.bin": "ROWS", "logbook_since.bin": "LOGBOOK",
    "areas.bin": "AREAS", "tags.bin": "TAGS", "membership.bin": "MEMBERSHIP",
    "project_areas.bin": "PROJECT_AREAS", "find_created_diag.bin": "FIND_CREATED", "probe.bin": "PROBE",
    "automation.bin": "AUTOMATION", "app_version.bin": "APP_VERSION",
}


def test_every_fixture_is_mapped():
    assert {p.name for p in FIXTURES.glob("*.bin")} == set(FIXTURE_TEMPLATES) | {"truncated_scan.bin"}


@pytest.mark.parametrize("name, template", FIXTURE_TEMPLATES.items())
def test_fixture_samples_parse(name, template):
    raw = fixture(name)
    assert raw.endswith(b"\n")
    f = parse_frame(raw, template)
    assert f.header[:3] == ["H", "tb1", a.HEADER_NAMES[template]]
    assert {r[0] for r in f.rows} <= a.RECORD_TAGS[template]


def test_truncated_fixture_is_rejected():
    with pytest.raises(ProtocolError, match="truncated"):
        parse_frame(fixture("truncated_scan.bin"), "SCAN")


def test_missing_trailing_newline_is_fine():
    assert parse_frame(scan(s_row(), newline=False), "SCAN").of("S") == [s_row()]


def test_trailing_newline_stripped_exactly_once():
    raw = scan(s_row())
    assert raw.endswith(b"\x1e" + b"E\x1f1\n")
    with pytest.raises(ProtocolError, match="count"):
        parse_frame(raw + b"\n", "SCAN")                      # "1\n" is not a count


def test_last_field_newline_is_data_not_the_osascript_newline():
    """Only the final byte is osascript's; a notes field ending in newlines keeps all of them."""
    f = parse_frame(scan(s_row(notes="a\n\n"), s_row(id=PROJ, notes="\n")), "SCAN")
    assert [r[6] for r in f.of("S")] == ["a\n\n", "\n"]


def test_carriage_returns_preserved():
    notes = "one\rtwo\r\nthree\n\r"
    f = parse_frame(scan(s_row(name="t\r", notes=notes)), "SCAN")
    assert f.of("S")[0][2] == "t\r"
    assert f.of("S")[0][6] == notes


def test_python_whitespace_and_line_breaks_survive():
    """\\x1c-\\x1f, \\x85, \\u2028 … are whitespace/line breaks to str.strip()/splitlines()."""
    odd = "  \x1c\x1d\x0b\x0c\x85  \t lead and trail \t"
    f = parse_frame(scan(s_row(name=odd, notes=odd)), "SCAN")
    assert f.of("S")[0][2] == odd and f.of("S")[0][6] == odd


def test_utf8_round_trip():
    title = "ünïcødé 🙂 & / + # = % , @ “q” -tb"
    assert parse_frame(scan(s_row(name=title)), "SCAN").of("S")[0][2] == title


@pytest.mark.parametrize("raw", [
    b"",
    b"\n",
    b"H\x1ftb1\x1fscan\x1flist\x1ftoday\x1f7200\n",                     # header only
    scan(s_row())[:-6],                                                # cut inside the E record
    scan(s_row()).replace(b"H\x1ftb1", b"H\x1ftb2"),                   # other protocol
    scan(s_row()).replace(b"H\x1ftb1", b"X\x1ftb1"),                   # no header
    scan(s_row()) + b"\x1eS\x1fx",                                     # data after E
    b"Things3 got an error\n",
    b"\x1e".join([b"H\x1ftb1\x1fscan", b"E\x1f0"])[1:],                 # H record damaged
])
def test_truncated_or_malformed(raw):
    with pytest.raises(ProtocolError):
        parse_frame(raw, "SCAN")


def test_count_mismatch():
    with pytest.raises(ProtocolError, match="count mismatch"):
        parse_frame(scan(s_row(), s_row(id=PROJ), count="3"), "SCAN")
    with pytest.raises(ProtocolError, match="count mismatch"):
        parse_frame(scan(s_row(), s_row(id=PROJ), count="1"), "SCAN")


@pytest.mark.parametrize("count", ["02", " 2", "2 ", "two", "", "-2"])
def test_count_must_be_the_exact_number(count):
    with pytest.raises(ProtocolError):
        parse_frame(scan(s_row(), s_row(id=PROJ), count=count), "SCAN")


def test_context_rows_are_not_counted():
    f = parse_frame(fixture("rows_mixed.bin"), "ROWS")
    assert len(f.rows) == 6 and len([r for r in f.rows if r[0] != "R"]) == 4
    f = parse_frame(fixture("find_created_diag.bin"), "FIND_CREATED")
    assert len(f.of("N")) == 2 and len(f.of("D")) == 1
    with pytest.raises(ProtocolError, match="count mismatch"):         # R counted -> 6 != 4
        parse_frame(fixture("rows_mixed.bin").replace(b"E\x1f4", b"E\x1f6"), "ROWS")


def test_empty_frames():
    f = parse_frame(frame(["H", "tb1", "scan", "app", "", "0"], ["E", "0"]), "SCAN")
    assert f.rows == [] and f.header == ["H", "tb1", "scan", "app", "", "0"]
    f = parse_frame(frame(["H", "tb1", "membership", "0"], ["M", "inbox"], ["E", "1"]), "MEMBERSHIP")
    assert a.membership_from_frame(f) == {"inbox": []}


FIXED = {"S": 7, "T": 16, "A": 4, "G": 5, "R": 5, "PA": 3, "N": 3, "D": 3, "X": 2}
TAG_TEMPLATE = {"S": "SCAN", "T": "ROWS", "A": "AREAS", "G": "TAGS", "R": "ROWS", "PA": "PROJECT_AREAS",
                "N": "FIND_CREATED", "D": "FIND_CREATED", "X": "ROWS"}


def test_arity_table_matches_design():
    assert dict(a.ARITY) == {**FIXED, "E": 2}
    assert dict(a.MIN_ARITY) == {"H": 3, "M": 2, "K": 3}
    assert len(a.ROW_T) == 16 and len(a.ROW_S) == 7


@pytest.mark.parametrize("tag", list(FIXED))
@pytest.mark.parametrize("delta", [-1, +1])
def test_fixed_arity_errors(tag, delta):
    template = TAG_TEMPLATE[tag]
    row = [tag] + ["x"] * (FIXED[tag] - 1 + delta)
    primary = "0" if tag in ("R", "D") else "1"
    raw = frame(["H", "tb1", a.HEADER_NAMES[template], "0"], row, ["E", primary])
    with pytest.raises(ProtocolError, match="fields"):
        parse_frame(raw, template)


def test_fixed_arity_ok_for_each_tag():
    for tag, n in FIXED.items():
        template = TAG_TEMPLATE[tag]
        primary = "0" if tag in ("R", "D") else "1"
        raw = frame(["H", "tb1", a.HEADER_NAMES[template], "0"], [tag] + ["1"] * (n - 1), ["E", primary])
        assert parse_frame(raw, template).rows == [[tag] + ["1"] * (n - 1)]


@pytest.mark.parametrize("raw, template", [
    (frame(["H", "tb1", "membership", "0"], ["M"], ["E", "1"]), "MEMBERSHIP"),
    (frame(["H", "tb1", "probe", "0"], ["K", "version"], ["E", "1"]), "PROBE"),
    (frame(["H", "tb1", "probe", "0"], ["E", "0", "x"]), "PROBE"),
    (frame(["H", "tb1", "probe", "0"], ["E"]), "PROBE"),
    (frame(["H", "tb1"], ["E", "0"]), "PROBE"),
])
def test_variable_arity_minimums(raw, template):
    with pytest.raises(ProtocolError):
        parse_frame(raw, template)


def test_variable_arity_records_accept_extra_fields():
    f = parse_frame(fixture("probe.bin"), "PROBE")
    assert [len(r) for r in f.of("K")] == [4, 5, 5, 4, 6]


@pytest.mark.parametrize("row", [["Q", "x"], ["P", *["x"] * 15], ["H", "tb1", "scan"], ["E", "1"], [""]])
def test_unknown_or_misplaced_record_tags(row):
    with pytest.raises(ProtocolError):
        parse_frame(scan(s_row(), row, count="2"), "SCAN")


def test_record_the_template_never_emits():
    with pytest.raises(ProtocolError, match="unexpected 'S'"):
        parse_frame(frame(["H", "tb1", "rows", "0"], s_row(), ["E", "1"]), "ROWS")
    with pytest.raises(ProtocolError, match="unexpected 'T'"):
        parse_frame(frame(["H", "tb1", "scan", "app", "", "0"], t_row(), ["E", "1"]), "SCAN")


def test_header_must_name_the_template():
    with pytest.raises(ProtocolError, match="'rows'"):
        parse_frame(fixture("rows_mixed.bin"), "SCAN")


def test_unregistered_template_name_skips_template_checks():
    f = parse_frame(frame(["H", "tb1", "whatever", "0"], s_row(), ["A", AREA, "Family", ""], ["E", "2"]), "adhoc")
    assert f.template == "adhoc" and len(f.rows) == 2


def test_not_utf8():
    with pytest.raises(ProtocolError, match="UTF-8"):
        parse_frame(scan(s_row()).replace(b"Buy", b"\xff\xfe"), "SCAN")


def test_text_input_is_a_programming_error():
    with pytest.raises(TypeError):
        parse_frame(scan(s_row()).decode(), "SCAN")               # runner must pass bytes (no text=True)
    assert parse_frame(bytearray(scan(s_row())), "SCAN").of("S") == [s_row()]


def test_gmt_mismatch_is_only_logged(caplog):
    offset = datetime.now().astimezone().utcoffset()
    ours = int(offset.total_seconds()) if offset else 0
    with caplog.at_level(logging.DEBUG, logger="things_bridge.applescript"):
        parse_frame(frame(["H", "tb1", "areas", str(ours)], ["E", "0"]), "AREAS")
        assert caplog.records == []
        f = parse_frame(frame(["H", "tb1", "areas", str(ours + 3600)], ["E", "0"]), "AREAS")
    assert f.rows == []
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    assert "time to GMT" in caplog.records[0].getMessage()


# ---- field conversion ---------------------------------------------------------------------------

@pytest.mark.parametrize("s, expected", [("", None), ("2026-09-28", "2026-09-28"), ("2024-02-29", "2024-02-29"),
                                         ("0999-01-01", "0999-01-01")])
def test_parse_day(s, expected):
    assert a.parse_day(s) == expected


@pytest.mark.parametrize("s", ["2026-02-30", "2025-02-29", "2026-9-28", "999-01-01", "2026-09-28T00:00:00",
                               " 2026-09-28", "2026/09/28", "missing value", "٢٠٢٦-٠٩-٢٨"])
def test_parse_day_rejects(s):
    with pytest.raises(ProtocolError):
        a.parse_day(s)


@pytest.mark.parametrize("s, expected", [
    ("2026-01-15T12:00:00", "2026-01-15T12:00:00+01:00"),          # CET
    ("2026-07-15T12:00:00", "2026-07-15T12:00:00+02:00"),          # CEST
    ("2026-03-29T01:59:59", "2026-03-29T01:59:59+01:00"),          # last second before spring-forward
    ("2026-03-29T03:00:00", "2026-03-29T03:00:00+02:00"),          # first second after
    ("2026-10-25T01:59:59", "2026-10-25T01:59:59+02:00"),
    ("2026-10-25T03:00:00", "2026-10-25T03:00:00+01:00"),
    ("2026-10-25T02:30:00", "2026-10-25T02:30:00+02:00"),          # repeated hour: first occurrence
    ("2026-12-31T23:59:59", "2026-12-31T23:59:59+01:00"),
])
def test_parse_stamp_dst_berlin(berlin, s, expected):
    """Each timestamp gets the offset valid on its own date, not the current one (§4.4)."""
    assert a.parse_stamp(s) == expected


def test_parse_stamp_follows_the_process_zone(tz):
    tz("UTC")
    assert a.parse_stamp("2026-07-15T12:00:00") == "2026-07-15T12:00:00+00:00"
    tz("America/New_York")
    if time.localtime(1_768_478_400).tm_gmtoff != -18000:
        pytest.skip("America/New_York zone data not available")
    assert a.parse_stamp("2026-01-15T12:00:00") == "2026-01-15T12:00:00-05:00"
    assert a.parse_stamp("2026-07-15T12:00:00") == "2026-07-15T12:00:00-04:00"


def test_parse_stamp_empty_is_none():
    assert a.parse_stamp("") is None


@pytest.mark.parametrize("s", ["2026-09-28", "2026-09-28 12:00:00", "2026-09-28T12:00", "2026-09-28T25:00:00",
                               "2026-02-30T00:00:00", "2026-09-28T12:00:00+02:00", "2026-09-28T12:00:00Z",
                               "2026-09-28T12:00:00.5", "Monday, 28 September 2026 at 12:00:00"])
def test_parse_stamp_rejects(s):
    with pytest.raises(ProtocolError):
        a.parse_stamp(s)


@pytest.mark.parametrize("s, expected", [
    ("Home, Mac", ["Home", "Mac"]),                    # OFF-AS example
    ("", []),
    ("Solo", ["Solo"]),
    ("Deep Work, Errand", ["Deep Work", "Errand"]),
    ("a,b", ["a", "b"]),
    (" spaced ,  tags ", ["spaced", "tags"]),
    ("a, , b,", ["a", "b"]),
    ("Café, 🙂 Fun, Ärger", ["Café", "🙂 Fun", "Ärger"]),
    ("Tab\t, x", ["Tab\t", "x"]),                      # only spaces are separators' padding
])
def test_split_tags(s, expected):
    assert a.split_tags(s) == expected


def test_notes_view():
    long = "x" * NOTES_PREVIEW_CHARS + "tail"
    exact = "y" * NOTES_PREVIEW_CHARS
    assert a.notes_view(long, "none") == (None, False)
    assert a.notes_view(long, "full") == (long, False)
    assert a.notes_view(long, "preview") == ("x" * NOTES_PREVIEW_CHARS, True)
    assert a.notes_view(exact, "preview") == (exact, False)
    assert a.notes_view("", "preview") == ("", False)
    assert a.notes_view("🥛" * 300, "preview") == ("🥛" * 200, True)     # code points, not bytes
    with pytest.raises(ValueError):
        a.notes_view("x", "summary")


# ---- row -> model conversion --------------------------------------------------------------------

def test_items_from_rows_fixture(berlin):
    f = parse_frame(fixture("rows_mixed.bin"), "ROWS")
    todo, project = a.items_from_frame(f, notes_mode="full")
    assert todo == Item(
        id=TODO, kind="to-do", title="Buy milk 🥛", status="open", tags=["Errand", "Home"],
        deadline="2026-10-02", start_date="2026-09-28",
        project=Ref(id=PROJ, title="Plan Birthday Party"),
        area=Ref(id=AREA, title="Family"),                             # derived through the project
        notes=NOTES, notes_truncated=False,
        created="2026-01-20T08:15:00+01:00", modified="2026-09-27T21:04:05+02:00",
        completed_at=None, canceled_at=None, link=link_for(TODO))
    assert project == Item(
        id=PROJ, kind="project", title="Plan Birthday Party", status="open", tags=[], deadline=None,
        start_date=None, project=None, area=Ref(id=AREA, title="Family"), notes="", notes_truncated=False,
        created="2026-03-01T10:00:00+01:00", modified="2026-09-01T10:00:00+02:00", completed_at=None,
        canceled_at=None, link=link_for(PROJ))
    assert todo.link.endswith("show?id=" + TODO)
    assert a.areas_from_frame(f) == [Area(id=AREA, title="Family", tags=["Home"], link=link_for(AREA))]
    assert a.missing_ids(f) == ["GJJVZHE7SNu7xcVuH2xDDh"]


def test_items_from_logbook_fixture(berlin):
    f = parse_frame(fixture("logbook_since.bin"), "LOGBOOK")
    assert f.header == ["H", "tb1", "logbook", "523", "2026-09-21", "7200"]
    done, cancelled = a.items_from_frame(f, notes_mode="preview")
    assert (done.status, done.completed_at, done.canceled_at) == ("completed", "2026-09-27T18:30:00+02:00", None)
    assert done.project == Ref(PROJ, "Plan Birthday Party") and done.area == Ref(AREA, "Family")
    assert done.notes == "Deposit paid"
    assert (cancelled.status, cancelled.completed_at) == ("canceled", None)
    assert cancelled.canceled_at == "2026-09-22T07:00:00+02:00"
    assert cancelled.created == "2025-12-24T23:59:59+01:00"
    assert cancelled.project is None and cancelled.area == Ref(AREA, "Family")     # area directly
    assert cancelled.tags == ["Someday"]


@pytest.mark.parametrize("mode, notes, truncated", [
    ("none", None, False),
    ("preview", "n" * NOTES_PREVIEW_CHARS, True),
    ("full", "n" * 250, False),
])
def test_items_notes_modes(mode, notes, truncated):
    f = rows_frame(t_row(notes="n" * 250))
    (item,) = a.items_from_frame(f, notes_mode=mode)
    assert (item.notes, item.notes_truncated) == (notes, truncated)


def test_item_container_edge_cases():
    other = "Qw3rTy8uIoPaSdFgHjKlZx"
    f = rows_frame(
        t_row(id=TODO, project_id=PROJ),                            # no R row for it: title unknown
        t_row(id=other, project_id="", area_id=AREA),               # area but no R row either
        t_row(id="Zz9Yy8Xx7Ww6Vv5Uu4Tt3S", project_id="", area_id=""),
    )
    first, second, third = a.items_from_frame(f, notes_mode="none")
    assert first.project == Ref(PROJ, "") and first.area is None
    assert second.project is None and second.area == Ref(AREA, "")
    assert third.project is None and third.area is None


def test_to_do_own_area_wins_over_project_area():
    f = rows_frame(t_row(project_id=PROJ, area_id="OwnArea000000000000000"),
                   ["R", "project", PROJ, "P", AREA], ["R", "area", AREA, "Family", ""],
                   ["R", "area", "OwnArea000000000000000", "Own", ""])
    (item,) = a.items_from_frame(f, notes_mode="none")
    assert item.area == Ref("OwnArea000000000000000", "Own")


def test_items_are_one_per_row_in_order():
    ids = ["Aa1Aa1Aa1Aa1Aa1Aa1Aa1A", "Bb2Bb2Bb2Bb2Bb2Bb2Bb2B", "Aa1Aa1Aa1Aa1Aa1Aa1Aa1A"]
    f = rows_frame(*(t_row(id=i) for i in ids))
    assert [i.id for i in a.items_from_frame(f, notes_mode="none")] == ids   # dedupe is the service's job


@pytest.mark.parametrize("row, match", [
    (t_row(status="unknown"), "status"),
    (t_row(status="Open"), "status"),
    (t_row(kind="area"), "kind"),
    (t_row(kind="todo"), "kind"),
    (t_row(id=""), "id"),
    (t_row(due_day="2026-13-01"), "date"),
    (t_row(start_day="tomorrow"), "date"),
    (t_row(created_ts="2026-09-28"), "timestamp"),
])
def test_items_reject_malformed_rows(row, match):
    with pytest.raises(ProtocolError, match=match):
        a.items_from_frame(rows_frame(row), notes_mode="full")


def test_unknown_container_kind():
    f = rows_frame(t_row(), ["R", "heading", "H1", "x", ""])
    with pytest.raises(ProtocolError, match="container kind"):
        a.items_from_frame(f, notes_mode="none")


def test_scan_rows():
    rows = a.scan_rows(parse_frame(fixture("scan_list_today.bin"), "SCAN"))
    assert rows[0] == a.ScanRow(id=TODO, title="Buy milk 🥛", status="open", tags=["Errand", "Home"],
                                deadline="2026-10-02", notes=NOTES)
    assert rows[1] == a.ScanRow(id="4mGzJwVqAsXS1HxUEXk2P5", title="Call doctor", status="open", tags=[],
                                deadline=None, notes="")
    assert (rows[2].title, rows[2].status, rows[2].notes) == ("  Pay rent  ", "completed", "line1\nline2\n")
    with pytest.raises(ProtocolError, match="status"):
        a.scan_rows(parse_frame(scan(s_row(status="unknown")), "SCAN"))


def test_areas_from_frame():
    areas = a.areas_from_frame(parse_frame(fixture("areas.bin"), "AREAS"))
    assert areas == [Area(AREA, "Family", ["Home"], link_for(AREA)),
                     Area("9X2kT7pQwErTyUiOpAsDfG", "Work", [], link_for("9X2kT7pQwErTyUiOpAsDfG"))]


def test_tags_from_frame():
    tags = a.tags_from_frame(parse_frame(fixture("tags.bin"), "TAGS"))
    assert tags == [
        Tag("TGhome000000000000000A", "Home", None, "h"),
        Tag("TGerrand0000000000000B", "Errand", Ref("TGhome000000000000000A", "Home"), None),
        Tag("TGdeep00000000000000C", "Deep Work", None, None),
    ]
    orphan = parse_frame(frame(["H", "tb1", "tags", "0"], ["G", "T1xxxxxxxx", "Kid", "Gone0000000", ""],
                               ["E", "1"]), "TAGS")
    assert a.tags_from_frame(orphan)[0].parent == Ref("Gone0000000", "")


def test_membership_from_frame():
    m = a.membership_from_frame(parse_frame(fixture("membership.bin"), "MEMBERSHIP"))
    assert m == {"inbox": [], "today": [TODO, "4mGzJwVqAsXS1HxUEXk2P5"], "someday": [PROJ]}


def test_project_areas_from_frame():
    pa = a.project_areas_from_frame(parse_frame(fixture("project_areas.bin"), "PROJECT_AREAS"))
    assert pa == {PROJ: AREA, "5hN8pL2qRsTuVwXyZaBcDe": None}


def test_find_created_rows_and_diagnostic():
    f = parse_frame(fixture("find_created_diag.bin"), "FIND_CREATED")
    assert f.header == ["H", "tb1", "find-created", "to-do", "list", "today", "7200"]
    assert a.created_from_frame(f) == [("6pQrStUvWxYzAbCdEfGhIj", 2), ("3kLmNoPqRsTuVwXyZaBcDe", 0)]
    assert a.diag_from_frame(f) == (42, 3)
    none = parse_frame(frame(["H", "tb1", "find-created", "to-do", "app", "", "0"], ["E", "0"]), "FIND_CREATED")
    assert a.created_from_frame(none) == [] and a.diag_from_frame(none) is None
    bad = parse_frame(frame(["H", "tb1", "find-created", "to-do", "app", "", "0"], ["N", TODO, "2.5"],
                            ["E", "1"]), "FIND_CREATED")
    with pytest.raises(ProtocolError, match="integer"):
        a.created_from_frame(bad)
    negative = parse_frame(frame(["H", "tb1", "find-created", "to-do", "app", "", "0"], ["N", TODO, "-3"],
                                 ["E", "1"]), "FIND_CREATED")
    assert a.created_from_frame(negative) == [(TODO, -3)]       # clock skew tolerated by the template


def test_probe_results():
    probe = a.probe_results(parse_frame(fixture("probe.bin"), "PROBE"))
    assert probe[0] == ("version", True, ["3.22.1"])
    assert probe[2] == ("list:trash", False,
                        ["Things3 got an error: Can’t get list id \"TMTrashListSource\".", "-1728"])
    assert a.probe_results(parse_frame(fixture("automation.bin"), "AUTOMATION")) == [("automation", True, ["5"])]
    assert a.probe_results(parse_frame(fixture("app_version.bin"), "APP_VERSION")) == \
        [("app-version", True, ["3.22.1", "1"])]
    bad = parse_frame(frame(["H", "tb1", "probe", "0"], ["K", "x", "yes"], ["E", "1"]), "PROBE")
    with pytest.raises(ProtocolError):
        a.probe_results(bad)


# ---- osascript errors ---------------------------------------------------------------------------

@pytest.mark.parametrize("stderr, code, message", [
    ('30:86: execution error: Things3 got an error: Can’t get to do id "x". (-1728)\n',
     -1728, 'Things3 got an error: Can’t get to do id "x".'),
    ("execution error: Not authorized to send Apple events to Things3. (-1743)\n",
     -1743, "Not authorized to send Apple events to Things3."),
    ("0:120: execution error: Things3 got an error: AppleEvent timed out. (-1712)\n",
     -1712, "Things3 got an error: AppleEvent timed out."),
    ("12:18: syntax error: Expected end of line but found identifier. (-2741)\n",
     -2741, "Expected end of line but found identifier."),
    ("execution error: Things3 got an error: Application isn’t running. (-600)", -600,
     "Things3 got an error: Application isn’t running."),
    ("4210:4263: execution error: Things 3 is not running (9001)\n", 9001, "Things 3 is not running"),
    ("5:9: execution error: column length mismatch (9101)\r\n", 9101, "column length mismatch"),
    ("30:86: execution error: Things3 got an error: Cannot move to-do (301)", 301,
     "Things3 got an error: Cannot move to-do"),
    ("osascript: warning: something\n1:2: execution error: boom (-10004)\n", -10004, "boom"),
    ("osascript: couldn't read script\n", None, "osascript: couldn't read script"),
    ("", None, ""),
    ("execution error: the value (42) is odd (-1700)", -1700, "the value (42) is odd"),
])
def test_parse_osa_error(stderr, code, message):
    assert a.parse_osa_error(stderr) == (code, message)


# ---- argv encoding and the registry -------------------------------------------------------------

def test_encode_args_prefixes_every_value():
    assert a.encode_args(["list", "", "-x", "--help", "=", "ünï 🙂", "a b\nc\r"]) == \
        ["=list", "=", "=-x", "=--help", "==", "=ünï 🙂", "=a b\nc\r"]
    assert a.encode_args(()) == []
    assert a.encode_args(("0", "1")) == ["=0", "=1"]


def test_encode_args_rejects_nul_and_bad_text():
    with pytest.raises(InvalidInput, match="argument 2 contains a NUL"):
        a.encode_args(["ok", "a\x00b"])
    with pytest.raises(InvalidInput, match="Unicode"):
        a.encode_args(["\ud800"])


def test_encode_args_size_limits():
    at_limit = "x" * (a.MAX_ARG_BYTES - 1)                      # + "=" = 64 KiB exactly
    assert a.encode_args([at_limit]) == ["=" + at_limit]
    with pytest.raises(InvalidInput, match="too long"):
        a.encode_args([at_limit + "x"])
    with pytest.raises(InvalidInput, match="too long"):
        a.encode_args(["é" * (a.MAX_ARG_BYTES // 2)])            # 2 bytes each in UTF-8
    four = [at_limit] * 4
    assert len(a.encode_args(four)) == 4                          # 256 KiB exactly
    with pytest.raises(InvalidInput, match="together"):
        a.encode_args(four + [""])
    with pytest.raises(InvalidInput) as e:
        a.encode_args(["secret" * 20000])
    assert "secret" not in str(e.value)                           # values are never echoed


def test_encode_args_type_errors():
    with pytest.raises(TypeError):
        a.encode_args("abc")
    with pytest.raises(TypeError):
        a.encode_args([1])
    with pytest.raises(TypeError):
        a.encode_args([b"x"])


def test_script_source():
    assert a.script_source("SCAN") == a.LIBRARY + "\n" + a.TEMPLATES["SCAN"].body
    assert a.script_source("APP_VERSION") == a.TEMPLATES["APP_VERSION"].body
    assert a.script_source("AREAS").count("on run argv") == 1
    with pytest.raises(KeyError):
        a.script_source("on run argv\n\tdo shell script \"x\"\nend run")


def test_registry_is_immutable():
    with pytest.raises(TypeError):
        a.TEMPLATES["EVIL"] = a.Template("EVIL", "on run argv\nend run")      # type: ignore[index]
    with pytest.raises(TypeError):
        del a.TEMPLATES["SCAN"]                                                # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.TEMPLATES["SCAN"].body = "x"                                         # type: ignore[misc]
    for table in (a.HEADER_NAMES, a.RECORD_TAGS, a.ARITY, a.MIN_ARITY):
        with pytest.raises(TypeError):
            table["X"] = 1                                                     # type: ignore[index]


def test_template_table_matches_design():
    """§4.3 argv and timeouts."""
    expected = {
        "SCAN": (70, 4, 4), "ROWS": (60, 2, 201), "LOGBOOK": (150, 3, 3), "PROJECT_AREAS": (60, 0, 0),
        "AREAS": (40, 0, 0), "TAGS": (40, 0, 0), "MEMBERSHIP": (60, 1, 8), "FIND_CREATED": (30, 6, 6),
        "PROBE": (90, 0, 1), "AUTOMATION": (25, 0, 0), "APP_VERSION": (15, 0, 0),
    }
    assert {n: (t.timeout_s, t.min_args, t.max_args) for n, t in a.TEMPLATES.items()} == expected
    assert set(a.HEADER_NAMES) == set(a.RECORD_TAGS) == set(a.TEMPLATES)
    assert a.BUNDLE_ID == "com.culturedcode.ThingsMac" and (a.US, a.RS) == ("\x1f", "\x1e")


def test_check_args():
    a.TEMPLATES["SCAN"].check_args(["list", "today", "0", "0"])
    a.TEMPLATES["ROWS"].check_args(["1"] + [TODO] * 200)
    a.TEMPLATES["PROBE"].check_args([])
    a.TEMPLATES["PROBE"].check_args([PROJ])
    with pytest.raises(InvalidInput, match="SCAN takes 4 arguments, got 3"):
        a.TEMPLATES["SCAN"].check_args(["list", "today", "0"])
    with pytest.raises(InvalidInput, match="ROWS takes 2 to 201 arguments, got 202"):
        a.TEMPLATES["ROWS"].check_args(["1"] + [TODO] * 201)
    with pytest.raises(InvalidInput, match="got 1"):
        a.TEMPLATES["ROWS"].check_args(["1"])
    with pytest.raises(InvalidInput):
        a.TEMPLATES["AREAS"].check_args(["x"])
    unbounded = a.Template("T", "", min_args=1)
    unbounded.check_args(["x"] * 500)
    with pytest.raises(InvalidInput, match="at least 1"):
        unbounded.check_args([])
