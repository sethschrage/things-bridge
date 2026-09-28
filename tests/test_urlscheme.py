import copy
import dataclasses
import inspect
import os
import pickle
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote

import pytest

from things_bridge import urlscheme as us
from things_bridge.errors import InvalidInput
from things_bridge.jsoncmd import Todo
from things_bridge.urlscheme import (
    CLEAR,
    ThingsURL,
    build_add,
    build_add_project,
    build_json,
    build_search,
    build_show,
    build_update,
    build_update_project,
    build_version,
    compose_when,
    enc,
    normalize_deadline,
    normalize_reminder,
    resolve_day,
)

TID = "SyJEz273ceSkabUbciM73A"          # OFF-URL to-do id (22 chars)
PID = "Jvj7EW1fLoScPhaw2JomCT"          # OFF-URL project id
AREA = "Lg8UqVPXo2SbJNiBpDBBQ"          # OFF-URL area id (21 chars)
HEALTH = "TryhwrjdiHEXfjgNtw81yt"
UUID = "6D5B8F2A-1C3E-4B7D-9F0A-2E4C6B8D0F1A"
TODAY = date(2026, 9, 28)


def query(url: str) -> list[tuple[str, str]]:
    """Decode a things:/// URL's query the way Things does (no '+' -> space)."""
    _, _, q = url.partition("?")
    return [(k, unquote(v)) for k, _, v in (p.partition("=") for p in q.split("&"))] if q else []


# ---- golden vectors (§7, exact strings from LOCAL golden.py) -----------------------------------

GOLDEN = [
    (lambda: build_add(title="Buy milk", notes="High fat"),
     "things:///add?title=Buy%20milk&notes=High%20fat"),
    (lambda: build_add(title="Buy milk", notes="Low fat.", when="evening", tags=["Errand"]),
     "things:///add?title=Buy%20milk&notes=Low%20fat.&when=evening&tags=Errand"),
    (lambda: build_add(titles=["Milk", "Beer", "Cheese"], list="Shopping"),
     "things:///add?titles=Milk%0ABeer%0ACheese&list=Shopping"),
    (lambda: build_add(title="Call doctor", when="next monday", list_id=HEALTH, strict=False),
     "things:///add?title=Call%20doctor&when=next%20monday&list-id=TryhwrjdiHEXfjgNtw81yt"),
    (lambda: build_add(title="Collect dry cleaning", when="evening@6pm"),
     "things:///add?title=Collect%20dry%20cleaning&when=evening@6pm"),
    (lambda: build_add_project(title="Build treehouse", when="today"),
     "things:///add-project?title=Build%20treehouse&when=today"),
    (lambda: build_add_project(title="Plan Birthday Party", area="Family"),
     "things:///add-project?title=Plan%20Birthday%20Party&area=Family"),
    (lambda: build_add_project(title="Submit Tax", deadline="December 31", area_id=AREA, strict=False),
     "things:///add-project?title=Submit%20Tax&deadline=December%2031&area-id=Lg8UqVPXo2SbJNiBpDBBQ"),
    (lambda: build_update(id=TID, when="today"),
     "things:///update?id=SyJEz273ceSkabUbciM73A&when=today"),
    (lambda: build_update(id=TID, title="Buy bread"),
     "things:///update?id=SyJEz273ceSkabUbciM73A&title=Buy%20bread"),
    (lambda: build_update(id=TID, append_notes="Wholemeal bread"),
     "things:///update?id=SyJEz273ceSkabUbciM73A&append-notes=Wholemeal%20bread"),
    (lambda: build_update(id=TID, append_checklist_items=["Cheese", "Bread", "Eggplant"]),
     "things:///update?id=SyJEz273ceSkabUbciM73A&append-checklist-items=Cheese%0ABread%0AEggplant"),
    (lambda: build_update(id=TID, deadline=CLEAR),
     "things:///update?id=SyJEz273ceSkabUbciM73A&deadline="),
    (lambda: build_update_project(id=PID, when="tomorrow"),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&when=tomorrow"),
    (lambda: build_update_project(id=PID, add_tags=["Important"]),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&add-tags=Important"),
    (lambda: build_update_project(id=PID, prepend_notes="SFO to JFK."),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&prepend-notes=SFO%20to%20JFK."),
    (lambda: build_update_project(id=PID, deadline=CLEAR),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&deadline="),
    (lambda: build_show(id="today"), "things:///show?id=today"),
    (lambda: build_show(id="GJJVZHE7SNu7xcVuH2xDDh"), "things:///show?id=GJJVZHE7SNu7xcVuH2xDDh"),
    (lambda: build_show(query="vacation"), "things:///show?query=vacation"),
    (lambda: build_show(query="vacation", filter=["errand"]), "things:///show?query=vacation&filter=errand"),
    (lambda: build_search(query="vacation"), "things:///search?query=vacation"),
    (lambda: build_search(), "things:///search"),
    (lambda: build_version(), "things:///version"),
    (lambda: build_json([Todo(title="Buy milk")]),
     "things:///json?data=%5B%7B%22type%22%3A%22to-do%22%2C%22operation%22%3A%22create%22%2C%22attributes%22"
     "%3A%7B%22title%22%3A%22Buy%20milk%22%7D%7D%5D"),
]


@pytest.mark.parametrize("factory,expected", GOLDEN, ids=[g[1][10:60] for g in GOLDEN])
def test_golden_urls(factory, expected):
    assert factory().url == expected


# Verbatim OFF-URL examples (whitespace from the doc's layout removed). Hex case differs (%0a), so
# compare decoded forms (RFC 3986: hex case is insignificant).
OFFICIAL = [
    (lambda: build_add(title="Buy milk", notes="High fat"), "things:///add?title=Buy%20milk&notes=High%20fat"),
    (lambda: build_add(title="Book flights"), "things:///add?title=Book%20flights"),
    (lambda: build_add(title="Buy milk", notes="Low fat.", when="evening", tags=["Errand"]),
     "things:///add?title=Buy%20milk&notes=Low%20fat.&when=evening&tags=Errand"),
    (lambda: build_add(titles=["Milk", "Beer", "Cheese"], list="Shopping"),
     "things:///add?titles=Milk%0aBeer%0aCheese&list=Shopping"),
    (lambda: build_add(title="Call doctor", when="next monday", list_id=HEALTH, strict=False),
     "things:///add?title=Call%20doctor&when=next%20monday&list-id=TryhwrjdiHEXfjgNtw81yt"),
    (lambda: build_add(title="Collect dry cleaning", when="evening@6pm"),
     "things:///add?title=Collect%20dry%20cleaning&when=evening@6pm"),
    (lambda: build_add_project(title="Build treehouse", when="today"),
     "things:///add-project?title=Build%20treehouse&when=today"),
    (lambda: build_add_project(title="Plan Birthday Party", area="Family"),
     "things:///add-project?title=Plan%20Birthday%20Party&area=Family"),
    (lambda: build_add_project(title="Submit Tax", deadline="December 31", area_id=AREA, strict=False),
     "things:///add-project?title=Submit%20Tax&deadline=December%2031&area-id=Lg8UqVPXo2SbJNiBpDBBQ"),
    (lambda: build_update(id=TID, when="today"), "things:///update?id=SyJEz273ceSkabUbciM73A&when=today"),
    (lambda: build_update(id=TID, title="Buy bread"),
     "things:///update?id=SyJEz273ceSkabUbciM73A&title=Buy%20bread"),
    (lambda: build_update(id=TID, append_notes="Wholemeal bread"),
     "things:///update?id=SyJEz273ceSkabUbciM73A&append-notes=Wholemeal%20bread"),
    (lambda: build_update(id=TID, append_checklist_items=["Cheese", "Bread", "Eggplant"]),
     "things:///update?id=SyJEz273ceSkabUbciM73A&append-checklist-items=Cheese%0aBread%0aEggplant"),
    (lambda: build_update(id=TID, deadline=CLEAR), "things:///update?id=SyJEz273ceSkabUbciM73A&deadline="),
    (lambda: build_update_project(id=PID, when="tomorrow"),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&when=tomorrow"),
    (lambda: build_update_project(id=PID, add_tags=["Important"]),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&add-tags=Important"),
    (lambda: build_update_project(id=PID, prepend_notes="SFO to JFK."),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&prepend-notes=SFO%20to%20JFK."),
    (lambda: build_update_project(id=PID, deadline=CLEAR),
     "things:///update-project?id=Jvj7EW1fLoScPhaw2JomCT&deadline="),
    (lambda: build_show(id="today"), "things:///show?id=today"),
    (lambda: build_show(id="GJJVZHE7SNu7xcVuH2xDDh"), "things:///show?id=GJJVZHE7SNu7xcVuH2xDDh"),
    (lambda: build_show(id="Qi9pM1heCNAZxKREgQrwnJ"), "things:///show?id=Qi9pM1heCNAZxKREgQrwnJ"),
    (lambda: build_show(query="vacation"), "things:///show?query=vacation"),
    (lambda: build_show(query="vacation", filter=["errand"]), "things:///show?query=vacation&filter=errand"),
    (lambda: build_search(query="vacation"), "things:///search?query=vacation"),
    (lambda: build_search(), "things:///search"),
    (lambda: build_version(), "things:///version"),
]


@pytest.mark.parametrize("factory,official", OFFICIAL, ids=[o[1][10:60] for o in OFFICIAL])
def test_unquote_equals_verbatim_official_examples(factory, official):
    ours = factory().url
    assert unquote(ours) == unquote(official)
    assert ours.lower() == official.lower()          # only the hex case may differ


# ---- allowlists ---------------------------------------------------------------------------------

# OFF-URL "Parameters" sections, transcribed in doc order (auth-token included where the doc lists it).
OFFICIAL_PARAMS = {
    "add": ["title", "titles", "notes", "when", "deadline", "tags", "checklist-items", "use-clipboard", "list-id",
            "list", "heading-id", "heading", "completed", "canceled", "show-quick-entry", "reveal", "creation-date",
            "completion-date"],
    "add-project": ["title", "notes", "when", "deadline", "tags", "area-id", "area", "to-dos", "completed",
                    "canceled", "reveal", "creation-date", "completion-date"],
    "update": ["auth-token", "id", "title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tags",
               "add-tags", "checklist-items", "prepend-checklist-items", "append-checklist-items", "list-id", "list",
               "heading-id", "heading", "completed", "canceled", "reveal", "duplicate", "creation-date",
               "completion-date"],
    "update-project": ["auth-token", "id", "title", "notes", "prepend-notes", "append-notes", "when", "deadline",
                       "tags", "add-tags", "area-id", "area", "completed", "canceled", "reveal", "duplicate",
                       "creation-date", "completion-date"],
    "show": ["id", "query", "filter"],
    "search": ["query"],
    "version": [],
    "json": ["auth-token", "data", "reveal"],
}


def test_param_allowlists_match_official_doc():
    assert us.ALLOWED_COMMANDS == tuple(OFFICIAL_PARAMS)
    assert tuple(us.PARAMS) == us.ALLOWED_COMMANDS
    for command, keys in OFFICIAL_PARAMS.items():
        assert us.PARAMS[command] == tuple(k for k in keys if k != "auth-token"), command


def _parse_official_doc(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    command, in_params = None, False
    for line in text.splitlines():
        m = re.match(r"^# `([a-z-]+)` command", line)
        if m:
            command, in_params = m.group(1), False
            out[command] = []
        elif line.startswith("## "):
            in_params = line.strip() == "## Parameters"
        elif command and in_params and (m := re.fullmatch(r"`([a-z-]+)`", line.strip())):
            out[command].append(m.group(1))
    return out


@pytest.mark.skipif(not os.environ.get("TB_OFFICIAL_URL_SCHEME_DOC"),
                    reason="set TB_OFFICIAL_URL_SCHEME_DOC to a text copy of culturedcode.com/things/support/articles/2803573")
def test_param_allowlists_match_official_doc_file():
    parsed = _parse_official_doc(Path(os.environ["TB_OFFICIAL_URL_SCHEME_DOC"]).read_text(encoding="utf-8"))
    assert parsed.pop("add-json") == []            # deprecated: documented without parameters, not allowed
    assert "add-json" not in us.ALLOWED_COMMANDS
    assert parsed == OFFICIAL_PARAMS


def test_allowlists_are_immutable_and_consistent():
    assert isinstance(us.PARAMS, MappingProxyType)
    with pytest.raises(TypeError):
        us.PARAMS["delete"] = ("id",)  # type: ignore[index]
    assert all(isinstance(v, tuple) for v in us.PARAMS.values())
    assert isinstance(us.ALLOWED_COMMANDS, tuple)
    assert us.MUTATING == {"add", "add-project", "update", "update-project", "json"}
    assert us.FOREGROUND == {"show", "search"}
    assert us.TOKEN_ALWAYS == {"update", "update-project"}
    assert us.MUTATING | us.FOREGROUND | {"version"} == set(us.ALLOWED_COMMANDS)
    for keys in us.PARAMS.values():
        assert "auth-token" not in keys
        assert not any(k.startswith("x-") for k in keys)
        assert len(set(keys)) == len(keys)
    assert us.SHOW_BUILTIN_IDS == ("inbox", "today", "anytime", "upcoming", "someday", "logbook", "tomorrow",
                                   "deadlines", "repeating", "all-projects", "logged-projects")
    assert (us.RATE_BUDGET, us.RATE_WINDOW_S, us.MAX_CHECKLIST) == (200, 10.0, 100)
    assert (us.MAX_STRING_UTF16, us.MAX_NOTES_CODEPOINTS, us.MAX_URL_BYTES) == (4000, 10_000, 512 * 1024)


BUILDERS = {
    "add": build_add,
    "add-project": build_add_project,
    "update": build_update,
    "update-project": build_update_project,
    "show": build_show,
    "search": build_search,
}


@pytest.mark.parametrize("command", list(BUILDERS))
def test_builder_keywords_map_one_to_one_to_params(command):
    sig = inspect.signature(BUILDERS[command])
    names = [n for n in sig.parameters if n != "strict"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    assert tuple(n.replace("_", "-") for n in names) == us.PARAMS[command]


@pytest.mark.parametrize("fn", [*BUILDERS.values(), build_version, build_json])
def test_no_token_or_callback_parameters(fn):
    for name in inspect.signature(fn).parameters:
        assert not re.search(r"token|auth", name, re.I)
        assert not name.startswith("x_")


def test_unknown_keyword_is_a_type_error():
    with pytest.raises(TypeError):
        build_add(title="x", auth_token="secret")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_update(title="x")  # type: ignore[call-arg]   # id is required


def test_params_emitted_in_doc_order_regardless_of_call_order():
    turl = build_add(tags=["A"], reveal=True, notes="n", title="t", when="today")
    assert [k for k, _ in turl.params] == ["title", "notes", "when", "tags", "reveal"]


# ---- ThingsURL ------------------------------------------------------------------------------------

def test_things_url_is_frozen_hashable_and_derives_url():
    turl = build_update(id=TID, title="A & B")
    assert isinstance(turl, ThingsURL)
    assert turl == ThingsURL("update", (("id", TID), ("title", "A & B")), 1, True)
    assert turl.params == (("id", TID), ("title", "A & B"))
    assert turl.url == f"things:///update?id={TID}&title=A%20%26%20B"
    with pytest.raises(dataclasses.FrozenInstanceError):
        turl.command = "add"  # type: ignore[misc]
    assert hash(turl) == hash(build_update(id=TID, title="A & B"))
    assert turl == build_update(id=TID, title="A & B")
    assert "auth-token" not in turl.url
    assert turl.url.startswith(us.SCHEME_PREFIX + "update?")


@pytest.mark.parametrize("factory,weight,token,mutating,foreground", [
    (lambda: build_add(title="x"), 1, False, True, False),
    (lambda: build_add(titles=["a", "b", "c"]), 3, False, True, False),
    (lambda: build_add_project(title="p"), 1, False, True, False),
    (lambda: build_add_project(title="p", to_dos=["a", "b", "c", "d"]), 5, False, True, False),
    (lambda: build_update(id=TID, title="x"), 1, True, True, False),
    (lambda: build_update_project(id=PID, title="x"), 1, True, True, False),
    (lambda: build_show(id="today"), 0, False, False, True),
    (lambda: build_search(query="x"), 0, False, False, True),
    (lambda: build_search(), 0, False, False, True),
    (lambda: build_version(), 0, False, False, False),
    (lambda: build_json([Todo(title="a"), Todo(title="b")]), 2, False, True, False),
    (lambda: build_json([Todo(operation="update", id=TID, when="anytime")]), 1, True, True, False),
])
def test_weight_token_and_mode(factory, weight, token, mutating, foreground):
    turl = factory()
    assert (turl.weight, turl.requires_token, turl.mutating, turl.foreground) == (weight, token, mutating, foreground)


# ---- encoding -------------------------------------------------------------------------------------

def test_enc_reserved_characters():
    assert enc("& = / # + , @ \n %") == "%26%20%3D%20%2F%20%23%20%2B%20%2C%20%40%20%0A%20%25"
    assert enc("?;:[]!$'()*") == "%3F%3B%3A%5B%5D%21%24%27%28%29%2A"


def test_enc_unreserved_and_non_ascii():
    assert enc("AZaz09-._~") == "AZaz09-._~"
    assert enc("ü") == "%C3%BC"
    assert enc("🙂é") == "%F0%9F%99%82%C3%A9"
    assert enc("12€") == "12%E2%82%AC"


def test_space_is_never_plus():
    url = build_add(title="C++ a b", notes="x y").url
    assert "title=C%2B%2B%20a%20b" in url
    assert "+" not in url


def test_enc_normalises_newlines_and_rejects_nul_and_surrogates():
    assert enc("a\r\nb\rc\nd") == "a%0Ab%0Ac%0Ad"
    with pytest.raises(InvalidInput, match="NUL"):
        enc("a\x00b")
    with pytest.raises(InvalidInput, match="surrogate"):
        enc("a\ud800b")


def test_enc_safe_parameter():
    assert enc("evening@6:30pm", safe="@:") == "evening@6:30pm"
    assert enc("evening@6:30pm") == "evening%406%3A30pm"


def test_crlf_normalised_in_params_and_url():
    turl = build_add(title="x", notes="line1\r\nline2\rline3")
    assert dict(turl.params)["notes"] == "line1\nline2\nline3"
    assert "notes=line1%0Aline2%0Aline3" in turl.url


@pytest.mark.parametrize("field", ["title", "notes", "list", "heading"])
def test_nul_rejected_everywhere(field):
    kwargs = {"title": "x", "list_id": PID} | {field: "a\x00b"}
    with pytest.raises(InvalidInput, match="NUL"):
        build_add(**kwargs)


TRICKY = "A & B/2+3 #x=y ü @:;? %20 %0A 100% \"q\" 'q' <tag> 🙂"


def test_tricky_values_round_trip():
    turl = build_add(title=TRICKY, notes=TRICKY + "\nsecond line", tags=["C++", "a/b", "ü 🙂"], list="R&D #1")
    assert query(turl.url) == list(turl.params)
    assert dict(query(turl.url))["title"] == TRICKY
    assert dict(query(turl.url))["tags"] == "C++,a/b,ü 🙂"


def test_when_keeps_at_and_colon_bare_but_titles_do_not():
    url = build_add(title="a@b:c", when="2026-02-25@14:00").url
    assert "title=a%40b%3Ac" in url
    assert url.endswith("when=2026-02-25@14:00")
    assert "when=in%203%20days" in build_add(title="x", when="in 3 days", strict=False).url


# ---- CLEAR and empty values -------------------------------------------------------------------------

def test_clear_is_a_singleton():
    assert repr(CLEAR) == "CLEAR"
    assert copy.copy(CLEAR) is CLEAR
    assert copy.deepcopy(CLEAR) is CLEAR
    assert pickle.loads(pickle.dumps(CLEAR)) is CLEAR


@pytest.mark.parametrize("key", ["notes", "deadline", "tags", "when"])
def test_clear_allowed_on_updates(key):
    assert build_update(id=TID, **{key: CLEAR}).url == f"things:///update?id={TID}&{key}="
    assert build_update_project(id=PID, **{key: CLEAR}).url == f"things:///update-project?id={PID}&{key}="


@pytest.mark.parametrize("builder", [build_add, build_add_project])
@pytest.mark.parametrize("key", ["notes", "deadline", "tags", "when"])
def test_clear_refused_on_creates(builder, key):
    with pytest.raises(InvalidInput, match="only possible with update"):
        builder(title="x", **{key: CLEAR})


@pytest.mark.parametrize("key", ["title", "prepend_notes", "append_notes", "add_tags", "checklist_items",
                                 "prepend_checklist_items", "append_checklist_items", "list_id", "list",
                                 "heading_id", "heading", "completed", "canceled", "reveal", "duplicate"])
def test_clear_refused_for_other_update_fields(key):
    with pytest.raises(InvalidInput, match="can't be cleared"):
        build_update(id=TID, **{key: CLEAR})


def test_clear_refused_on_navigation():
    with pytest.raises(InvalidInput):
        build_show(query="x", filter=CLEAR)  # type: ignore[arg-type]
    with pytest.raises(InvalidInput):
        build_search(query=CLEAR)  # type: ignore[arg-type]


def test_empty_notes_and_tags_clear_on_update_and_are_omitted_on_create():
    assert build_update(id=TID, notes="").url.endswith("&notes=")
    assert build_update(id=TID, tags=[]).url.endswith("&tags=")
    assert build_update_project(id=PID, tags=()).url.endswith("&tags=")
    assert build_add(title="x", notes="", tags=[]).url == "things:///add?title=x"
    assert build_add_project(title="x", notes="", tags=[], to_dos=[]).url == "things:///add-project?title=x"
    assert build_update(id=TID, title="x", add_tags=[], prepend_notes="").params == (("id", TID), ("title", "x"))


def test_empty_when_or_deadline_needs_explicit_clear():
    for key in ("when", "deadline"):
        with pytest.raises(InvalidInput):
            build_update(id=TID, **{key: ""})


# ---- limits -----------------------------------------------------------------------------------------

def test_title_limit_in_utf16_units():
    build_add(title="a" * 4000)
    with pytest.raises(InvalidInput, match="4001 UTF-16 units"):
        build_add(title="a" * 4001)
    emoji = "🙂" * 2000                                   # 2000 code points, 4000 UTF-16 units
    assert us.utf16_len(emoji) == 4000
    build_add(title=emoji)
    with pytest.raises(InvalidInput, match="UTF-16"):
        build_add(title=emoji + "a")                    # len() == 2001 but 4001 units


@pytest.mark.parametrize("builder,kwargs", [
    (build_add, {"titles": ["ok", "a" * 4001]}),
    (build_add, {"title": "x", "checklist_items": ["a" * 4001]}),
    (build_add_project, {"title": "x", "to_dos": ["a" * 4001]}),
    (build_add, {"title": "x", "list": "a" * 4001}),
    (build_add, {"title": "x", "list_id": PID, "heading": "a" * 4001}),
    (build_add_project, {"title": "x", "area": "a" * 4001}),
    (build_add, {"title": "x", "tags": ["a" * 4001]}),
    (build_show, {"query": "a" * 4001}),
    (build_update, {"id": TID, "append_checklist_items": ["🙂" * 2001]}),
])
def test_line_and_name_limits(builder, kwargs):
    with pytest.raises(InvalidInput, match="UTF-16"):
        builder(**kwargs)


@pytest.mark.parametrize("key", ["notes", "prepend_notes", "append_notes"])
def test_notes_limit_in_code_points(key):
    build_update(id=TID, **{key: "n" * 10_000})
    build_update(id=TID, **{key: "🙂" * 10_000})       # 20,000 UTF-16 units, 10,000 code points
    with pytest.raises(InvalidInput, match="10001 characters"):
        build_update(id=TID, **{key: "n" * 10_001})


def test_add_notes_limit():
    build_add(title="x", notes="n" * 10_000)
    with pytest.raises(InvalidInput):
        build_add(title="x", notes="n" * 10_001)
    with pytest.raises(InvalidInput):
        build_add_project(title="x", notes="n" * 10_001)


@pytest.mark.parametrize("builder,key", [
    (build_add, "checklist_items"),
    (build_update, "prepend_checklist_items"),
    (build_update, "append_checklist_items"),
])
def test_checklist_limit(builder, key):
    base = {"id": TID} if builder is build_update else {"title": "x"}
    turl = builder(**base, **{key: [f"item {i}" for i in range(100)]})
    assert dict(turl.params)[key.replace("_", "-")].count("\n") == 99
    with pytest.raises(InvalidInput, match="101 items"):
        builder(**base, **{key: [f"item {i}" for i in range(101)]})


def test_url_size_limit(monkeypatch):
    lines = ["é" * 4000] * 60                            # each é is 6 encoded bytes: ~1.4 MB
    with pytest.raises(InvalidInput, match="bytes; the limit is 524288") as err:
        build_add(titles=lines)
    assert "split" in err.value.hint
    monkeypatch.setattr(us, "MAX_URL_BYTES", 40)
    build_add(title="x" * 10)                           # things:///add?title= + 10 = 30 bytes
    with pytest.raises(InvalidInput, match="limit is 40"):
        build_add(title="x" * 30)


# ---- structural validation ----------------------------------------------------------------------

@pytest.mark.parametrize("title", ["", "   ", "\t", "a\nb", "a\rb", "a\r\nb"])
def test_title_must_be_one_non_blank_line(title):
    with pytest.raises(InvalidInput):
        build_add(title=title)
    with pytest.raises(InvalidInput):
        build_update(id=TID, title=title)


@pytest.mark.parametrize("builder,kwargs", [
    (build_add, {"titles": []}),
    (build_add, {"titles": ["a", ""]}),
    (build_add, {"titles": ["a", "b\nc"]}),
    (build_add, {"title": "x", "checklist_items": ["ok", " "]}),
    (build_update, {"id": TID, "append_checklist_items": ["a\nb"]}),
    (build_add_project, {"title": "x", "to_dos": ["ok", ""]}),
])
def test_line_lists_validated(builder, kwargs):
    with pytest.raises(InvalidInput):
        builder(**kwargs)


def test_line_lists_must_be_sequences_and_join_with_newlines():
    for bad in ("a\nb", "Errand", {"a"}):
        with pytest.raises(InvalidInput, match="expected a list"):
            build_add(title="x", checklist_items=bad)
    assert build_add(title="x", checklist_items=("a", "b")).url.endswith("checklist-items=a%0Ab")
    assert dict(build_add_project(title="p", to_dos=["a", "b"]).params)["to-dos"] == "a\nb"


@pytest.mark.parametrize("tags,problem", [
    (["a,b"], "comma"),
    ([""], "blank"),
    ([" "], "blank"),
    (["a\nb"], "single line"),
    ("Errand", "expected a list"),
    ([1], "expected a string"),
])
def test_tag_validation(tags, problem):
    with pytest.raises(InvalidInput, match=problem):
        build_add(title="x", tags=tags)
    with pytest.raises(InvalidInput, match=problem):
        build_update(id=TID, add_tags=tags)
    with pytest.raises(InvalidInput, match=problem):
        build_show(query="x", filter=tags)


def test_tags_joined_with_commas():
    turl = build_update(id=TID, tags=["Home", "Mac", "Errand"])
    assert dict(turl.params)["tags"] == "Home,Mac,Errand"
    assert turl.url.endswith("tags=Home%2CMac%2CErrand")
    assert query(build_show(id="today", filter=["a", "b"]).url) == [("id", "today"), ("filter", "a,b")]


@pytest.mark.parametrize("good", [TID, AREA, UUID, "abcd1234", "A" * 64])
def test_valid_ids(good):
    assert dict(build_update(id=good, title="x").params)["id"] == good


@pytest.mark.parametrize("bad", ["", "short", "abc1234", "-abcdefgh", "abc def ghi", TID + "\n", "\n" + TID,
                                 "a" * 65, "today", "../../etc/pw", "ab/cdefghi", "abcdefgh%", "abcdéfghij"])
def test_invalid_ids_refused(bad):
    with pytest.raises(InvalidInput, match="not a Things id"):
        build_update(id=bad, title="x")


@pytest.mark.parametrize("builder,key", [
    (build_add, "list_id"), (build_add_project, "area_id"),
    (build_update, "list_id"), (build_update, "heading_id"), (build_update_project, "area_id"),
])
def test_container_ids_validated(builder, key):
    base = {"id": TID} if builder in (build_update, build_update_project) else {"title": "x"}
    extra = {"list_id": PID} if key == "heading_id" else {}
    with pytest.raises(InvalidInput, match="not a Things id"):
        builder(**base, **extra, **{key: "Shopping list"})


@pytest.mark.parametrize("builtin", us.SHOW_BUILTIN_IDS)
def test_show_accepts_builtin_list_ids(builtin):
    assert build_show(id=builtin).url == f"things:///show?id={builtin}"


def test_show_id_rules():
    with pytest.raises(InvalidInput):
        build_show(id="trash")
    with pytest.raises(InvalidInput, match="exactly one"):
        build_show()
    with pytest.raises(InvalidInput, match="exactly one"):
        build_show(id="today", query="vacation")
    with pytest.raises(InvalidInput, match="exactly one"):
        build_show(filter=["errand"])
    with pytest.raises(InvalidInput, match="blank"):
        build_show(query=" ")


def test_search_query():
    assert build_search(query="shipping address").url == "things:///search?query=shipping%20address"
    with pytest.raises(InvalidInput):
        build_search(query="")
    with pytest.raises(InvalidInput):
        build_search(query="a\nb")


EXCLUSIVE_CASES = [
    (build_add, {"title": "a", "titles": ["b"]}),
    (build_add, {"title": "a", "list": "Work", "list_id": PID}),
    (build_add, {"title": "a", "list_id": PID, "heading": "H", "heading_id": HEALTH}),
    (build_add, {"title": "a", "completed": True, "canceled": True}),
    (build_add, {"title": "a", "completed": True, "canceled": False}),
    (build_add_project, {"title": "a", "area": "Family", "area_id": AREA}),
    (build_add_project, {"title": "a", "completed": False, "canceled": True}),
    (build_update, {"id": TID, "tags": ["a"], "add_tags": ["b"]}),
    (build_update, {"id": TID, "tags": CLEAR, "add_tags": ["b"]}),
    (build_update, {"id": TID, "notes": "a", "prepend_notes": "b"}),
    (build_update, {"id": TID, "notes": "a", "append_notes": "b"}),
    (build_update, {"id": TID, "notes": CLEAR, "append_notes": "b"}),
    (build_update, {"id": TID, "list": "Work", "list_id": PID}),
    (build_update, {"id": TID, "heading": "H", "heading_id": HEALTH}),
    (build_update, {"id": TID, "completed": True, "canceled": True}),
    (build_update_project, {"id": PID, "area": "Family", "area_id": AREA}),
    (build_update_project, {"id": PID, "tags": ["a"], "add_tags": ["b"]}),
    (build_update_project, {"id": PID, "notes": "a", "prepend_notes": "b"}),
]


@pytest.mark.parametrize("builder,kwargs", EXCLUSIVE_CASES)
def test_mutually_exclusive_parameters(builder, kwargs):
    with pytest.raises(InvalidInput, match="not both"):
        builder(**kwargs)


def test_non_conflicting_combinations_allowed():
    build_update(id=TID, prepend_notes="a", append_notes="b")
    build_update(id=TID, completed=False, canceled=False)
    build_update(id=TID, prepend_checklist_items=["a"], append_checklist_items=["b"])
    build_update(id=TID, list_id=PID, heading="Planning")


def test_booleans():
    assert build_update(id=TID, completed=True).url.endswith("completed=true")
    assert build_update(id=TID, completed=False).url.endswith("completed=false")
    assert build_add(title="x", reveal=False).url.endswith("reveal=false")
    for bad in ("true", 1, 0, "false"):
        with pytest.raises(InvalidInput, match="true or false"):
            build_update(id=TID, canceled=bad)


def test_heading_needs_a_project_on_add():
    with pytest.raises(InvalidInput, match="list-id"):
        build_add(title="x", heading="Planning")
    with pytest.raises(InvalidInput, match="list-id"):
        build_add(title="x", heading_id=HEALTH)
    assert query(build_add(title="x", list_id=PID, heading="Planning").url)[-1] == ("heading", "Planning")
    build_update(id=TID, heading="Planning")             # already in a project: allowed by the doc


def test_update_needs_id_and_a_change():
    with pytest.raises(InvalidInput, match="needs the id"):
        build_update(id=None, title="x")  # type: ignore[arg-type]
    with pytest.raises(InvalidInput, match="nothing to update"):
        build_update(id=TID)
    with pytest.raises(InvalidInput, match="nothing to update"):
        build_update(id=TID, reveal=True)
    with pytest.raises(InvalidInput, match="nothing to update"):
        build_update_project(id=PID, notes=None, tags=None)
    build_update(id=TID, duplicate=True)


def test_update_never_replaces_a_checklist():
    """D11: replacing destroys checklist items that can't be read back."""
    for value in (["a"], []):
        with pytest.raises(InvalidInput, match="never does") as err:
            build_update(id=TID, checklist_items=value)
        assert "append-checklist-items" in err.value.hint
    with pytest.raises(InvalidInput, match="can't be cleared"):
        build_update(id=TID, checklist_items=CLEAR)
    assert "checklist-items=a%0Ab" in build_add(title="x", checklist_items=["a", "b"]).url


# ---- when / deadline ------------------------------------------------------------------------------

WHEN_OK = ["today", "tomorrow", "evening", "someday", "2026-10-02", "2028-02-29", "evening@6pm", "today@6:30pm",
           "2026-02-25@14:00", "tomorrow@9:05am", "2026-10-02@0:00", "today@12am", "today@23:59"]
WHEN_BAD = ["next monday", "Today", "TODAY", "2026-02-30", "2027-02-29", "2026-13-01", "26-10-02", "2026-1-2",
            "today@25:00", "today@6:60", "today@13pm", "today@0am", "today@", "@6pm", "evening @6pm", "today@6 pm",
            "today@6PM", "noon", "anytime@6pm", "someday@6pm", "someday@18:00", "in 3 days", "yesterday"]


@pytest.mark.parametrize("when", WHEN_OK + ["anytime"])
def test_when_strict_accepts_canonical_on_create(when):
    assert dict(build_add(title="x", when=when).params)["when"] == when
    assert dict(build_add_project(title="x", when=when).params)["when"] == when


@pytest.mark.parametrize("when", WHEN_OK)
def test_when_strict_accepts_canonical_on_update(when):
    assert dict(build_update(id=TID, when=when).params)["when"] == when


@pytest.mark.parametrize("when", WHEN_BAD)
def test_when_strict_rejects(when):
    with pytest.raises(InvalidInput):
        build_add(title="x", when=when)


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("builder,target", [(build_update, TID), (build_update_project, PID)])
def test_anytime_refused_on_update(builder, target, strict):
    with pytest.raises(InvalidInput, match="anytime") as err:
        builder(id=target, when="anytime", strict=strict)
    assert "json" in err.value.hint
    if not strict:
        with pytest.raises(InvalidInput, match="anytime"):
            builder(id=target, when="Anytime", strict=False)


@pytest.mark.parametrize("when", ["someday@6pm", "anytime@18:00", "Someday@6pm", "someday @ 6pm"])
def test_time_with_someday_or_anytime_refused_even_non_strict(when):
    with pytest.raises(InvalidInput, match="reminder time"):
        build_add(title="x", when=when, strict=False)


def test_when_non_strict():
    assert dict(build_add(title="x", when="next tuesday", strict=False).params)["when"] == "next tuesday"
    build_add(title="x", when="w" * 100, strict=False)
    for bad in ("w" * 101, "", "  ", "next\ntuesday", "a\tb"):
        with pytest.raises(InvalidInput):
            build_add(title="x", when=bad, strict=False)


@pytest.mark.parametrize("deadline", ["today", "tomorrow", "2026-12-31", "2028-02-29", "2001-01-01"])
def test_deadline_strict_accepts(deadline):
    assert dict(build_add(title="x", deadline=deadline).params)["deadline"] == deadline
    assert dict(build_update(id=TID, deadline=deadline).params)["deadline"] == deadline


@pytest.mark.parametrize("deadline", ["December 31", "evening", "someday", "anytime", "2026-02-29", "2026-12-31@6pm",
                                      "Today", "31.12.2026"])
def test_deadline_strict_rejects(deadline):
    with pytest.raises(InvalidInput):
        build_add(title="x", deadline=deadline)


def test_deadline_non_strict():
    assert dict(build_add(title="x", deadline="December 31", strict=False).params)["deadline"] == "December 31"
    with pytest.raises(InvalidInput):
        build_add(title="x", deadline="d" * 101, strict=False)


# ---- dates, clipboard -------------------------------------------------------------------------------

def test_creation_and_completion_dates():
    plus1 = timezone(timedelta(hours=1))
    turl = build_add(title="x", completed=True, creation_date=datetime(2026, 3, 10, 14, 30, 0, 999_999, tzinfo=plus1),
                     completion_date=datetime(2026, 3, 11, 0, 0, tzinfo=timezone.utc))
    assert dict(turl.params)["creation-date"] == "2026-03-10T13:30:00Z"
    assert dict(turl.params)["completion-date"] == "2026-03-11T00:00:00Z"
    assert "creation-date=2026-03-10T13%3A30%3A00Z" in turl.url
    assert dict(build_update(id=TID, creation_date=datetime(2026, 1, 1, tzinfo=timezone.utc)).params)[
        "creation-date"] == "2026-01-01T00:00:00Z"
    for bad in (datetime(2026, 3, 10, 14, 30), "2026-03-10T14:30:00Z", date(2026, 3, 10)):
        with pytest.raises(InvalidInput):
            build_add_project(title="x", creation_date=bad)


def test_use_clipboard():
    for value in us.USE_CLIPBOARD_VALUES:
        assert dict(build_add(use_clipboard=value).params)["use-clipboard"] == value
    with pytest.raises(InvalidInput):
        build_add(use_clipboard="replace-everything")


# ---- errors ----------------------------------------------------------------------------------------

def test_errors_name_the_field():
    with pytest.raises(InvalidInput) as err:
        build_add(title="x", tags=["ok", "a,b"])
    assert err.value.message.startswith("tags[1]: ")
    assert err.value.details == {"field": "tags[1]"}
    assert err.value.code == "invalid_input"
    with pytest.raises(InvalidInput) as err:
        build_add(title=42)  # type: ignore[arg-type]
    assert err.value.message == "title: expected a string, got int"


# ---- normalizers ------------------------------------------------------------------------------------

REMINDER_OK = [
    ("18:00", "6:00pm"), ("6pm", "6:00pm"), ("6:30 PM", "6:30pm"), ("6:30PM", "6:30pm"), ("9:30PM", "9:30pm"),
    ("21:30", "9:30pm"), ("9:05am", "9:05am"), ("9:05 am", "9:05am"), ("12am", "12:00am"), ("12pm", "12:00pm"),
    ("12:30pm", "12:30pm"), ("12:30 AM", "12:30am"), ("0:15", "12:15am"), ("00:00", "12:00am"), ("06:45", "6:45am"),
    ("09:05", "9:05am"), ("13", "1:00pm"), ("23:59", "11:59pm"), ("  7PM ", "7:00pm"), ("11:05pm", "11:05pm"),
]


@pytest.mark.parametrize("raw,expected", REMINDER_OK)
def test_normalize_reminder(raw, expected):
    assert normalize_reminder(raw) == expected
    assert re.fullmatch(r"(1[0-2]|[1-9]):[0-5]\d(am|pm)", expected)


@pytest.mark.parametrize("raw,suggestion", [
    ("9:05", "say 9:05am or 21:05"), ("9", "say 9:00am or 21:00"), ("10:30", "say 10:30am or 22:30"),
    ("1", "say 1:00am or 13:00"), ("12:00", "say 12:00am or 12:00pm"),
])
def test_normalize_reminder_ambiguous(raw, suggestion):
    with pytest.raises(InvalidInput, match="ambiguous") as err:
        normalize_reminder(raw)
    assert suggestion in err.value.message


@pytest.mark.parametrize("raw", ["", " ", "noon", "24:00", "6:60pm", "13pm", "0am", "9:5", "6.30pm", "18h",
                                 "6 : 30", "25", "123", "6:30 P M", "-1:00", 5])
def test_normalize_reminder_invalid(raw):
    with pytest.raises(InvalidInput):
        normalize_reminder(raw)


@pytest.mark.parametrize("when,reminder,expected", [
    ("today", None, "today"),
    ("Evening", "18:00", "evening@6:00pm"),
    ("2026-10-02", "6pm", "2026-10-02@6:00pm"),
    ("2026-09-28", None, "2026-09-28"),
    (" tomorrow ", "9:05am", "tomorrow@9:05am"),
    ("anytime", None, "anytime"),
    ("someday", None, "someday"),
])
def test_compose_when(when, reminder, expected):
    composed = compose_when(when, reminder, today=TODAY)
    assert composed == expected
    assert us.check_when("when", composed, strict=True) == composed


@pytest.mark.parametrize("when,reminder,problem", [
    ("2026-09-27", None, "in the past"),
    ("anytime", "6pm", "anytime"),
    ("someday", "18:00", "someday"),
    ("today@6pm", None, "reminder_time"),
    ("next monday", None, "YYYY-MM-DD"),
    ("2026-02-30", None, "calendar"),
    ("tomorrow", "9:05", "ambiguous"),
    ("", None, "YYYY-MM-DD"),
])
def test_compose_when_rejects(when, reminder, problem):
    with pytest.raises(InvalidInput, match=problem):
        compose_when(when, reminder, today=TODAY)


def test_normalize_deadline():
    assert normalize_deadline("today", today=TODAY) == "today"
    assert normalize_deadline(" Tomorrow ", today=TODAY) == "tomorrow"
    assert normalize_deadline("2020-01-01", today=TODAY) == "2020-01-01"     # overdue deadlines are fine
    for bad in ("December 31", "2026-02-30", "evening", "2026-12-31@6pm", ""):
        with pytest.raises(InvalidInput):
            normalize_deadline(bad, today=TODAY)


def test_resolve_day():
    assert resolve_day("today", today=TODAY) == "2026-09-28"
    assert resolve_day("evening@6:00pm", today=TODAY) == "2026-09-28"
    assert resolve_day("tomorrow", today=date(2026, 12, 31)) == "2027-01-01"
    assert resolve_day("2026-10-02@6:00pm", today=TODAY) == "2026-10-02"
    assert resolve_day("anytime", today=TODAY) is None
    assert resolve_day("someday", today=TODAY) is None
    with pytest.raises(InvalidInput):
        resolve_day("next monday", today=TODAY)


# ---- version ---------------------------------------------------------------------------------------

def test_build_version_exists_but_is_never_dispatched_by_the_service():
    turl = build_version()
    assert (turl.url, turl.params, turl.weight, turl.mutating, turl.requires_token) == \
        ("things:///version", (), 0, False, False)
    service = Path(us.__file__).with_name("service.py")
    if not service.exists():
        pytest.skip("service.py not written yet")
    assert "build_version" not in service.read_text(encoding="utf-8")
