"""The cage's seccomp profile: Docker's default, with no setuid or setgid bit.

The default profile ships verbatim in agent_proxy/seccomp/default.json. The
cage's profile allows each call that takes a mode only when the mode holds
neither bit.
"""

import errno
import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from typing import cast

SETID_BITS = 0o6000
# O_CREAT | __O_TMPFILE on amd64: the kernel reads an open call's mode only
# when the flags hold one of the two.
CREATE_FLAGS = 0o20000100
_ALLOW = "SCMP_ACT_ALLOW"
_MODE_INDEXES = {
    "chmod": 1,
    "fchmod": 1,
    "creat": 1,
    "mknod": 1,
    "fchmodat": 2,
    "fchmodat2": 2,
    "mknodat": 2,
}
# (flags index, mode index) of each open call.
_OPEN_INDEXES = {"open": (1, 2), "openat": (2, 3)}
# Seccomp cannot read the mode inside openat2's struct; a caller that gets
# ENOSYS falls back to openat.
_OPENAT2_RULE = {
    "names": ["openat2"],
    "action": "SCMP_ACT_ERRNO",
    "errnoRet": errno.ENOSYS,
}
_GUARDED_NAMES = frozenset({*_MODE_INDEXES, *_OPEN_INDEXES, "openat2"})

type Rule = dict[str, object]


@cache
def cage_profile_json() -> str:
    """Return the cage's profile as compact JSON text, built once per process."""
    default = json.loads(
        files("agent_proxy.seccomp").joinpath("default.json").read_text("utf-8")
    )
    return json.dumps(derive_cage_profile(default), separators=(",", ":"))


def derive_cage_profile(default: Mapping[str, object]) -> dict[str, object]:
    """Return default with the setid bits refused to every call that sets a mode.

    Each call that takes a mode leaves every allow rule that has no argument
    check. Each comes back under one rule that checks its mode, and open and
    openat under a second rule that checks their flags. openat2 comes back
    refused with ENOSYS.
    """
    rules = cast(list[Rule], default["syscalls"])
    return {
        **default,
        "syscalls": [*map(_without_guarded_names, rules), *_guard_rules()],
    }


def _without_guarded_names(rule: Rule) -> Rule:
    if rule["action"] != _ALLOW or rule.get("args"):
        return rule
    names = cast(list[str], rule["names"])
    return {**rule, "names": [name for name in names if name not in _GUARDED_NAMES]}


def _guard_rules() -> list[Rule]:
    mode_rules = [
        _allow_clear(name, index, SETID_BITS) for name, index in _MODE_INDEXES.items()
    ]
    open_rules = [
        rule
        for name, (flags_index, mode_index) in _OPEN_INDEXES.items()
        for rule in (
            _allow_clear(name, mode_index, SETID_BITS),
            _allow_clear(name, flags_index, CREATE_FLAGS),
        )
    ]
    return [*mode_rules, *open_rules, _OPENAT2_RULE]


def _allow_clear(name: str, index: int, mask: int) -> Rule:
    """Allow name when its argument at index holds none of mask's bits."""
    return {
        "names": [name],
        "action": _ALLOW,
        "args": [
            {"index": index, "value": mask, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}
        ],
    }
