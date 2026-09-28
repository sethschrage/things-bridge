"""The auth token (§2.7, §5.2): env override, Keychain via ``/usr/bin/security``, stdin-only writes, ``TOKEN_RE``.

``subprocess.run`` is replaced by a recorder; nothing here touches a real Keychain.
"""

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from things_bridge import authtoken, runner
from things_bridge.authtoken import ACCOUNT, ENV, SECURITY, SERVICE, TOKEN_RE, get_token, set_token, token_source
from things_bridge.errors import InvalidInput, KeychainError, NotMacOS

TOKEN = "vKkylosuSuGwxrz7qcklOw"
ODD_TOKEN = "Ab+Cd/Ef=Gh~12.3_4-5"
FIND = [SECURITY, "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"]


def done(rc: int = 0, out: bytes | str = b"", err: bytes | str = b"") -> subprocess.CompletedProcess:
    enc = (lambda v: v.encode("utf-8") if isinstance(v, str) else v)
    return subprocess.CompletedProcess([], rc, enc(out), enc(err))


def times_out(argv, kw):
    raise subprocess.TimeoutExpired(argv, kw["timeout"])


def os_error(argv, kw):
    raise FileNotFoundError(2, "No such file or directory", argv[0])


@dataclass
class FakeSecurity:
    """Answers ``security`` calls from a queue and checks each call's hygiene."""
    answers: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and argv[0] == SECURITY
        assert not kw.get("shell") and kw.get("capture_output") is True
        assert ("input" in kw) != ("stdin" in kw)
        if "stdin" in kw:
            assert kw["stdin"] is subprocess.DEVNULL, "security must never read the MCP channel"
        assert not {"text", "encoding", "universal_newlines"} & set(kw)
        assert kw["timeout"] > 0 and ENV not in kw["env"]
        self.calls.append((list(argv), kw))
        if not self.answers:
            raise AssertionError(f"unexpected security call {argv[:2]}")
        answer = self.answers.pop(0)
        return answer(argv, kw) if callable(answer) else answer


@pytest.fixture
def sec(monkeypatch) -> FakeSecurity:
    fake = FakeSecurity()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(runner, "_secrets", ())
    return fake


# ---- TOKEN_RE -----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [TOKEN, ODD_TOKEN, "a" * 8, "Z" * 256, "abc-def_ghi.jkl~mno"])
def test_token_re_accepts(value):
    assert TOKEN_RE.fullmatch(value) and authtoken.is_valid_token(value)


@pytest.mark.parametrize("value", ["", "short77", "a" * 257, "has space inside", 'quo"te12345', "back\\slash123",
                                  "semi;colon1234", "ünïcødé-token", TOKEN + "\n", "tab\there12345", "a&b=c&d=eee"])
def test_token_re_rejects(value):
    assert not authtoken.is_valid_token(value)
    assert not authtoken.is_valid_token(None)


def test_constants():
    assert (SECURITY, SERVICE, ACCOUNT, ENV) == ("/usr/bin/security", "things-bridge", "things-url-auth-token",
                                                 "THINGS_AUTH_TOKEN")
    assert authtoken.NOT_FOUND_RC == 44


def test_no_keychain_deletion_anywhere():
    names = [n for n in dir(authtoken) if not n.startswith("__")]
    assert not [n for n in names if "del" in n.lower() or "remove" in n.lower() or "clear" in n.lower()]
    source = Path(authtoken.__file__).read_text()
    assert "delete-" not in source and "-generic-password -D" not in source


# ---- get_token / token_source -------------------------------------------------------------------

def test_env_wins_without_touching_the_keychain(sec, monkeypatch):
    monkeypatch.setenv(ENV, f"  {TOKEN}\n")
    assert get_token() == TOKEN
    assert token_source() == "env"
    assert sec.calls == []


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_env_falls_back_to_keychain(sec, monkeypatch, blank):
    monkeypatch.setenv(ENV, blank)
    sec.answers = [done(0, TOKEN + "\n")]
    assert get_token() == TOKEN
    assert [c[0] for c in sec.calls] == [FIND]


def test_keychain_read_shape(sec):
    sec.answers = [done(0, TOKEN + "\n"), done(0, TOKEN + "\n")]
    assert get_token() == TOKEN
    assert token_source() == "keychain"
    argv, kw = sec.calls[0]
    assert argv == FIND
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == authtoken.READ_TIMEOUT_S


def test_keychain_not_found_rc_44(sec):
    sec.answers = [done(44, err="security: SecKeychainSearchCopyNext: The specified item could not be found in the "
                                "keychain.\n"), done(44)]
    assert get_token() is None
    assert token_source() == "missing"


def test_keychain_empty_value_is_missing(sec):
    sec.answers = [done(0, "\n")]
    assert get_token() is None


def test_keychain_read_strips_only_newlines(sec):
    sec.answers = [done(0, TOKEN + "\n\n")]
    assert get_token() == TOKEN


@pytest.mark.parametrize("rc", [1, 36, 51, 128])
def test_keychain_other_rc_is_keychain_error(sec, rc):
    leak = "SECRET-FROM-STDOUT"
    sec.answers = [done(rc, out=leak, err="security: SecKeychainItemCopyContent: User interaction is not allowed.")]
    with pytest.raises(KeychainError) as e:
        get_token()
    err = e.value
    assert err.code == "keychain_error" and err.exit_code == 3
    assert "Couldn't read the Keychain" in err.message and f"exited {rc}" in err.message
    assert "User interaction is not allowed" in err.message
    assert err.hint == "keychain locked? run from a logged-in GUI session"
    assert leak not in err.message and leak not in repr(err) and leak not in str(err.details)
    assert err.__context__ is None


def test_keychain_error_redacts_stderr(sec):
    sec.answers = [done(9, err="weird things:///update?id=X&auth-token=zzzsecret")]
    with pytest.raises(KeychainError) as e:
        get_token()
    assert "zzzsecret" not in e.value.message and "auth-token=***" in e.value.message


@pytest.mark.parametrize("answer", [times_out, os_error])
def test_keychain_read_cannot_run(sec, answer):
    sec.answers = [answer]
    with pytest.raises(KeychainError) as e:
        get_token()
    assert e.value.__context__ is None and e.value.__cause__ is None
    assert e.value.message.startswith("Couldn't read the Keychain: security ")
    assert e.value.hint == "keychain locked? run from a logged-in GUI session"


def test_keychain_off_macos(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(NotMacOS):
        get_token()
    with pytest.raises(NotMacOS):
        token_source()
    monkeypatch.setenv(ENV, TOKEN)
    assert get_token() == TOKEN and token_source() == "env"
    assert calls == []


# ---- set_token ----------------------------------------------------------------------------------

def test_set_token_sends_the_token_only_on_stdin(sec, monkeypatch):
    monkeypatch.setenv(ENV, "env-token-not-this-one")
    sec.answers = [done(0), done(0, TOKEN + "\n")]
    set_token(TOKEN)
    (write_argv, write_kw), (read_argv, _read_kw) = sec.calls
    assert write_argv == [SECURITY, "-i"]
    assert all(TOKEN not in a for c in sec.calls for a in c[0]), "never in argv (visible in ps)"
    assert write_kw["input"] == (f'add-generic-password -U -s "{SERVICE}" -a "{ACCOUNT}" '
                                 f'-l "Things URL auth token" -w "{TOKEN}"\n').encode("ascii")
    assert write_kw["timeout"] == authtoken.WRITE_TIMEOUT_S and ENV not in write_kw["env"]
    assert read_argv == FIND, "read back from the Keychain itself, not the env override"


def test_set_token_strips_pasted_whitespace(sec):
    sec.answers = [done(0), done(0, ODD_TOKEN + "\n")]
    set_token(f"  {ODD_TOKEN}\r\n")
    assert f'-w "{ODD_TOKEN}"\n'.encode() in sec.calls[0][1]["input"]


def test_set_token_remembers_the_secret_for_redaction(sec):
    sec.answers = [done(0), done(0, TOKEN + "\n")]
    set_token(TOKEN)
    assert runner.redact(f"echo {TOKEN}") == "echo ***"


@pytest.mark.parametrize("bad", ["", "short", "has space in it", 'quote"injection1', "x" * 257,
                                 'ok12345" -w "other', "new\nline12345", "ünïcødé-token", 12345678, None])
def test_set_token_validates_before_anything_runs(sec, bad):
    with pytest.raises(InvalidInput) as e:
        set_token(bad)
    assert sec.calls == []
    assert e.value.details == {"field": "token"}
    if isinstance(bad, str) and bad:
        assert bad not in e.value.message and bad not in (e.value.hint or "")


@pytest.mark.parametrize("answer", [
    done(1, err='add-generic-password: returned -25308\n'),
    done(0, err="security: SecKeychainItemCreateFromContent (<default>): User interaction is not allowed.\n"
                "add-generic-password: returned -25308\n"),
])
def test_set_token_write_failure(sec, answer):
    sec.answers = [answer]
    with pytest.raises(KeychainError) as e:
        set_token(TOKEN)
    assert "Couldn't write the Keychain" in e.value.message
    assert "security add-generic-password" in e.value.hint and e.value.hint.rstrip(".").endswith("prompts for it)")
    assert len(sec.calls) == 1, "no fallback that puts the token in argv"


def test_set_token_failure_redacts_an_echoed_token(sec):
    sec.answers = [lambda argv, kw: done(1, out=kw["input"], err=kw["input"].decode() + " returned 1")]
    with pytest.raises(KeychainError) as e:
        set_token(ODD_TOKEN)
    for form in runner._forms(ODD_TOKEN):
        assert form not in e.value.message and form not in repr(e.value)


@pytest.mark.parametrize("answer", [times_out, os_error])
def test_set_token_cannot_run(sec, answer):
    sec.answers = [answer]
    with pytest.raises(KeychainError) as e:
        set_token(TOKEN)
    assert e.value.__context__ is None
    assert TOKEN not in e.value.message


@pytest.mark.parametrize("readback", [done(0, "different-token-1\n"), done(44)])
def test_set_token_read_back_mismatch(sec, readback):
    sec.answers = [done(0), readback]
    with pytest.raises(KeychainError) as e:
        set_token(TOKEN)
    assert "doesn't read back" in e.value.message and TOKEN not in e.value.message


def test_set_token_off_macos(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(NotMacOS):
        set_token(TOKEN)
    with pytest.raises(InvalidInput):                   # validation comes first on every OS
        set_token("bad")
    assert calls == []


def test_manual_command_keeps_the_token_out_of_argv():
    assert authtoken.MANUAL_COMMAND.endswith(" -w")     # security then prompts for the password itself
    assert f"-s {SERVICE}" in authtoken.MANUAL_COMMAND and f"-a {ACCOUNT}" in authtoken.MANUAL_COMMAND
