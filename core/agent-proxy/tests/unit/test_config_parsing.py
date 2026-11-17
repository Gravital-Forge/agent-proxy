from datetime import timedelta
from pathlib import Path

import pytest

from agent_proxy.config import ConfigError, DurationError, parse_duration, read_yaml
from agent_proxy.errors import ProxyError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("5m", timedelta(minutes=5)),
        ("2h", timedelta(hours=2)),
        ("3d", timedelta(days=3)),
    ],
)
def test_parse_duration_reads_each_unit(text: str, expected: timedelta) -> None:
    """Each of s, m, h, and d scales the count to the matching timedelta."""
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["0s", "-5m", "10", "10w", "1.5h", "", "10s\n"])
def test_parse_duration_refuses_a_malformed_duration(text: str) -> None:
    """A duration that does not match the pattern is refused, naming the value."""
    with pytest.raises(DurationError) as excinfo:
        parse_duration(text)

    assert repr(text) in str(excinfo.value)


@pytest.mark.parametrize("text", ["1000000000d", "1" * 5000 + "s"])
def test_parse_duration_refuses_a_count_too_large(text: str) -> None:
    """A count past timedelta's range or int's digit limit is a DurationError."""
    with pytest.raises(DurationError) as excinfo:
        parse_duration(text)

    assert repr(text) in str(excinfo.value)


def test_duration_error_is_a_proxy_error_and_a_value_error() -> None:
    """The operator command catches it as ProxyError; pydantic, as ValueError."""
    with pytest.raises(DurationError) as excinfo:
        parse_duration("10w")

    assert isinstance(excinfo.value, ProxyError)
    assert isinstance(excinfo.value, ValueError)


def test_config_error_joins_its_messages_one_per_line() -> None:
    """A ConfigError holds each message and prints them one per line."""
    error = ConfigError("first failure", "second failure")

    assert isinstance(error, ProxyError)
    assert error.errors == ("first failure", "second failure")
    assert str(error) == "first failure\nsecond failure"


def test_read_yaml_returns_the_mapping(tmp_path: Path) -> None:
    """A valid file yields its top-level mapping, nested values included."""
    path = tmp_path / "config.yaml"
    path.write_text("name: dev\nscopes:\n  read:\n    roots: [a, b]\n")

    assert read_yaml(path) == {"name": "dev", "scopes": {"read": {"roots": ["a", "b"]}}}


def _refusal(path: Path) -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        read_yaml(path)
    assert len(excinfo.value.errors) == 1
    assert repr(str(path)) in str(excinfo.value)
    return excinfo.value


def test_read_yaml_refuses_a_missing_file(tmp_path: Path) -> None:
    """A file that cannot be read is a ConfigError naming the file."""
    _refusal(tmp_path / "absent.yaml")


def test_read_yaml_refuses_malformed_yaml(tmp_path: Path) -> None:
    """A YAML syntax error is a ConfigError naming the file."""
    path = tmp_path / "config.yaml"
    path.write_text("name: [dev, prod\n")

    _refusal(path)


def test_read_yaml_refuses_a_duplicate_top_level_key(tmp_path: Path) -> None:
    """A key repeated at the top level is refused rather than last-one-wins."""
    path = tmp_path / "config.yaml"
    path.write_text("name: dev\nname: prod\n")

    error = _refusal(path)

    assert "duplicate key 'name'" in str(error)


def test_read_yaml_refuses_a_duplicate_key_two_levels_down(tmp_path: Path) -> None:
    """A key repeated in a nested mapping is refused too."""
    path = tmp_path / "config.yaml"
    path.write_text("scopes:\n  read:\n    timeout: 10s\n    timeout: 20s\n")

    error = _refusal(path)

    assert "duplicate key 'timeout'" in str(error)


@pytest.mark.parametrize(
    "source",
    ["name: [dev, prod\n", "name: dev\nname: prod\n"],
    ids=["malformed", "duplicate-key"],
)
def test_read_yaml_refusal_is_one_line(tmp_path: Path, source: str) -> None:
    """A YAML error reads as one line, so the operator command prints it as one."""
    path = tmp_path / "config.yaml"
    path.write_text(source)

    error = _refusal(path)

    assert all("\n" not in message for message in error.errors)


def test_read_yaml_refusal_keeps_the_key_verbatim(tmp_path: Path) -> None:
    """Flattening the error to one line leaves the space inside a key alone."""
    path = tmp_path / "config.yaml"
    path.write_text('"a  b": 1\n"a  b": 2\n')

    error = _refusal(path)

    assert "duplicate key 'a  b'" in str(error)


def test_read_yaml_refuses_a_merge_key_by_name(tmp_path: Path) -> None:
    """A merge key is refused with a message that names merge keys."""
    path = tmp_path / "config.yaml"
    path.write_text("base: &grant {timeout: 10s}\nother:\n  <<: *grant\n")

    error = _refusal(path)

    assert "YAML merge keys ('<<') are not supported" in str(error)


def test_read_yaml_loads_a_plain_alias(tmp_path: Path) -> None:
    """An alias outside a merge key loads as a copy of its anchor's value."""
    path = tmp_path / "config.yaml"
    path.write_text("base: &grant {timeout: 10s}\nother: *grant\n")

    assert read_yaml(path) == {
        "base": {"timeout": "10s"},
        "other": {"timeout": "10s"},
    }


def test_read_yaml_refuses_a_top_level_list(tmp_path: Path) -> None:
    """A document whose top level is not a mapping is refused."""
    path = tmp_path / "config.yaml"
    path.write_text("- dev\n- prod\n")

    error = _refusal(path)

    assert "list" in str(error)


def test_read_yaml_refuses_an_unhashable_key(tmp_path: Path) -> None:
    """A sequence used as a key is a ConfigError, not a TypeError."""
    path = tmp_path / "config.yaml"
    path.write_text("? [a, b]\n: 1\n")

    error = _refusal(path)

    assert "unhashable key" in str(error)
