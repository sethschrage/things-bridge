import inspect
import pickle

import pytest

from things_bridge import errors
from things_bridge.errors import (
    EXIT_NOT_VERIFIED,
    Ambiguous,
    AutomationDenied,
    AutomationTimeout,
    DispatchFailed,
    InvalidInput,
    KeychainError,
    NotFound,
    NotMacOS,
    ProtocolError,
    RateLimited,
    ReadFailed,
    Refused,
    SandboxBlocked,
    ThingsBridgeError,
    ThingsNotInstalled,
    ThingsNotRunning,
    TokenMissing,
)

# §2.1, verbatim.
TAXONOMY = [
    (InvalidInput, "invalid_input", 2),
    (NotFound, "not_found", 5),
    (Ambiguous, "ambiguous", 5),
    (Refused, "refused", 4),
    (NotMacOS, "not_macos", 3),
    (ThingsNotInstalled, "things_not_installed", 3),
    (ThingsNotRunning, "things_not_running", 3),
    (AutomationDenied, "automation_denied", 3),
    (AutomationTimeout, "automation_timeout", 3),
    (SandboxBlocked, "sandbox_blocked", 3),
    (TokenMissing, "token_missing", 3),
    (KeychainError, "keychain_error", 3),
    (RateLimited, "rate_limited", 1),
    (DispatchFailed, "dispatch_failed", 1),
    (ReadFailed, "read_failed", 1),
    (ProtocolError, "protocol_error", 1),
]


def _module_error_classes():
    return {
        obj for obj in vars(errors).values()
        if inspect.isclass(obj) and issubclass(obj, ThingsBridgeError) and obj is not ThingsBridgeError
    }


@pytest.mark.parametrize(("cls", "code", "exit_code"), TAXONOMY, ids=[c.__name__ for c, _, _ in TAXONOMY])
def test_codes_and_exit_codes(cls, code, exit_code):
    assert issubclass(cls, ThingsBridgeError)
    assert cls.code == code
    assert cls.exit_code == exit_code
    e = cls("boom")
    assert (e.code, e.exit_code) == (code, exit_code)


def test_base_defaults():
    assert issubclass(ThingsBridgeError, Exception)
    assert ThingsBridgeError.code == "error"
    assert ThingsBridgeError.exit_code == 1


def test_taxonomy_is_complete_and_codes_unique():
    assert _module_error_classes() == {cls for cls, _, _ in TAXONOMY}
    codes = [code for _, code, _ in TAXONOMY]
    assert len(codes) == len(set(codes))
    assert "error" not in codes


def test_exit_codes_follow_cli_table():
    # §2.12: 2 invalid input, 3 environment, 4 refused, 5 not found/ambiguous, 6 not verified, 1 other.
    by_exit = {}
    for cls, _, exit_code in TAXONOMY:
        by_exit.setdefault(exit_code, set()).add(cls)
    assert by_exit[2] == {InvalidInput}
    assert by_exit[4] == {Refused}
    assert by_exit[5] == {NotFound, Ambiguous}
    assert by_exit[3] == {NotMacOS, ThingsNotInstalled, ThingsNotRunning, AutomationDenied,
                          AutomationTimeout, SandboxBlocked, TokenMissing, KeychainError}
    assert set(by_exit) == {1, 2, 3, 4, 5}


def test_exit_not_verified():
    assert EXIT_NOT_VERIFIED == 6
    assert all(exit_code != EXIT_NOT_VERIFIED for _, _, exit_code in TAXONOMY)
    assert not isinstance(EXIT_NOT_VERIFIED, type)   # a constant, not an exception


def test_message_hint_details():
    e = Refused("not visible in any list", hint="open it with things_show",
                details={"open_children": [{"id": "abcdefgh1", "title": "x"}]})
    assert e.message == "not visible in any list"
    assert str(e) == "not visible in any list"
    assert e.args == ("not visible in any list",)
    assert e.hint == "open it with things_show"
    assert e.details == {"open_children": [{"id": "abcdefgh1", "title": "x"}]}


def test_defaults_and_details_copied():
    e = NotFound("no item")
    assert e.hint is None
    assert e.details == {}
    src = {"candidates": []}
    a = Ambiguous("two matches", details=src)
    a.details["extra"] = 1
    assert src == {"candidates": []}
    # Instances never share a details dict.
    b, c = NotFound("x"), NotFound("y")
    b.details["k"] = 1
    assert c.details == {}


def test_hint_and_details_are_keyword_only():
    with pytest.raises(TypeError):
        InvalidInput("msg", "hint")  # type: ignore[misc]


def test_to_dict_shape():
    e = ReadFailed("Things returned an empty list", hint="close the dialog", details={"osa_code": -1719})
    assert e.to_dict() == {
        "code": "read_failed",
        "message": "Things returned an empty list",
        "hint": "close the dialog",
        "details": {"osa_code": -1719},
    }
    assert TokenMissing("no token").to_dict() == {
        "code": "token_missing", "message": "no token", "hint": None, "details": {},
    }


def test_catch_by_base_class():
    with pytest.raises(ThingsBridgeError) as info:
        raise AutomationTimeout("Things didn't answer")
    assert info.value.code == "automation_timeout"
    assert info.value.exit_code == 3


def test_repr():
    assert repr(NotMacOS("this system is linux")) == "NotMacOS('this system is linux')"


@pytest.mark.parametrize("cls", [cls for cls, _, _ in TAXONOMY], ids=[c.__name__ for c, _, _ in TAXONOMY])
def test_pickle_round_trip(cls):
    e = cls("msg", hint="h", details={"k": [1, 2]})
    r = pickle.loads(pickle.dumps(e))
    assert type(r) is cls
    assert (r.message, r.hint, r.details, r.code, r.exit_code) == ("msg", "h", {"k": [1, 2]}, e.code, e.exit_code)
