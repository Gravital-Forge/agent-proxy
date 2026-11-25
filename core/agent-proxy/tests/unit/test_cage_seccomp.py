import hashlib
import json
from importlib.resources import files

import pytest

from agent_proxy.cage_seccomp import cage_profile_json, derive_cage_profile

DEFAULT_PROFILE_SHA256 = (
    "785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334"
)
GUARDED_NAMES = {
    "chmod",
    "fchmod",
    "fchmodat",
    "fchmodat2",
    "creat",
    "mknod",
    "mknodat",
    "open",
    "openat",
    "openat2",
}
MODE_INDEXES = {
    "chmod": 1,
    "fchmod": 1,
    "creat": 1,
    "mknod": 1,
    "fchmodat": 2,
    "fchmodat2": 2,
    "mknodat": 2,
}
# (flags index, mode index) of each open call.
OPEN_INDEXES = {"open": (1, 2), "openat": (2, 3)}
SETID_BITS = 0o6000
# O_CREAT | __O_TMPFILE on amd64.
CREATE_FLAGS = 0o20000100


def _vendored_bytes() -> bytes:
    return files("agent_proxy.seccomp").joinpath("default.json").read_bytes()


def _default() -> dict[str, object]:
    return json.loads(_vendored_bytes())


def _derived() -> dict[str, object]:
    return json.loads(cage_profile_json())


def _rules(profile: dict[str, object]) -> list[dict[str, object]]:
    rules = profile["syscalls"]
    assert isinstance(rules, list)
    return rules


def _rules_naming(name: str) -> list[dict[str, object]]:
    return [rule for rule in _rules(_derived()) if name in rule["names"]]


def _masked_clear(index: int, mask: int) -> dict[str, object]:
    return {
        "index": index,
        "value": mask,
        "valueTwo": 0,
        "op": "SCMP_CMP_MASKED_EQ",
    }


def _allow(name: str, *args: dict[str, object]) -> dict[str, object]:
    return {"names": [name], "action": "SCMP_ACT_ALLOW", "args": list(args)}


def test_the_vendored_profile_is_docker_s_default_byte_for_byte() -> None:
    """The packaged file has the SHA-256 of moby/profiles seccomp v0.2.4."""
    assert hashlib.sha256(_vendored_bytes()).hexdigest() == DEFAULT_PROFILE_SHA256


def test_no_rule_allows_a_guarded_call_without_an_argument_check() -> None:
    """Each guarded name left in an allow rule sits under an argument check."""
    unchecked = [
        name
        for rule in _rules(_derived())
        if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
        for name in rule["names"]
        if name in GUARDED_NAMES
    ]

    assert unchecked == []


@pytest.mark.parametrize(("name", "index"), MODE_INDEXES.items())
def test_a_mode_call_is_allowed_only_with_neither_setid_bit(
    name: str, index: int
) -> None:
    """The one rule for the call allows it when its mode masks to zero."""
    assert _rules_naming(name) == [_allow(name, _masked_clear(index, SETID_BITS))]


@pytest.mark.parametrize(("name", "indexes"), OPEN_INDEXES.items())
def test_an_open_call_is_allowed_with_a_clear_mode_or_no_create_flag(
    name: str, indexes: tuple[int, int]
) -> None:
    """One rule checks the mode for both bits, one checks the flags for both."""
    flags_index, mode_index = indexes

    assert _rules_naming(name) == [
        _allow(name, _masked_clear(mode_index, SETID_BITS)),
        _allow(name, _masked_clear(flags_index, CREATE_FLAGS)),
    ]


def test_openat2_answers_enosys() -> None:
    """The struct that holds openat2's mode is out of seccomp's reach."""
    assert _rules_naming("openat2") == [
        {"names": ["openat2"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38}
    ]


def test_every_other_rule_and_field_is_the_default_s() -> None:
    """Only the guarded names leave the default's rules; the added rules follow."""
    default = _default()
    derived = _derived()
    default_rules = _rules(default)
    derived_rules = _rules(derived)
    stripped = [
        {**rule, "names": [n for n in rule["names"] if n not in GUARDED_NAMES]}
        if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
        else rule
        for rule in default_rules
    ]

    assert {k: v for k, v in derived.items() if k != "syscalls"} == {
        k: v for k, v in default.items() if k != "syscalls"
    }
    assert derived_rules[: len(stripped)] == stripped
    assert {
        name for rule in derived_rules[len(stripped) :] for name in rule["names"]
    } == GUARDED_NAMES


def test_a_guarded_name_under_an_argument_check_or_another_action_stays() -> None:
    """Only an unchecked allow rule loses its guarded names."""
    checked = _allow("chmod", _masked_clear(1, 0o7000))
    refused = {"names": ["open"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1}
    unchecked = {"names": ["creat", "read"], "action": "SCMP_ACT_ALLOW"}

    derived = derive_cage_profile(
        {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": [checked, refused, unchecked]}
    )

    assert _rules(derived)[:3] == [
        checked,
        refused,
        {"names": ["read"], "action": "SCMP_ACT_ALLOW"},
    ]


def test_the_profile_is_built_once_per_process() -> None:
    """Every call returns the one JSON text."""
    assert cage_profile_json() is cage_profile_json()
