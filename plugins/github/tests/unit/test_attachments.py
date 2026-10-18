import json
import os
from pathlib import Path

import pytest
from github_plugin import attachments
from github_plugin.scripts import RefusalError

ACCEPTED = "png, jpg, jpeg, gif, webp, mp4, mov, or webm"
SETTING_OFF = "attach is off: GITHUB_PR_ATTACHMENTS is 'off'"
NO_ROOTS = "attach needs the grant's roots: AGENT_PROXY_ROOTS is not set"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make tmp_path/root the one root AGENT_PROXY_ROOTS names, the setting unset."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("AGENT_PROXY_ROOTS", json.dumps([str(root)]))
    monkeypatch.delenv("GITHUB_PR_ATTACHMENTS", raising=False)
    return root


def _file(path: Path) -> str:
    """Write a one-byte file at path, and return path as a string."""
    path.write_bytes(b"x")
    return str(path)


def _refusal(entries: list[tuple[str, str | None]]) -> str:
    """Return the reason attach_args refuses entries with."""
    with pytest.raises(RefusalError) as refused:
        attachments.attach_args(entries, os.environ)
    return str(refused.value)


def _outside(path: str) -> str:
    """Return the reason for a path outside the roots."""
    return f"attach path '{path}' resolves outside the grant's roots"


def _not_regular(path: str) -> str:
    """Return the reason for a path that is not a regular file."""
    return f"attach path '{path}' is not a regular file"


def _bad_extension(path: str) -> str:
    """Return the reason for a path without an accepted extension."""
    return f"attach path '{path}' is not a {ACCEPTED} file"


def test_the_setting_is_github_pr_attachments() -> None:
    """The on/off setting comes from GITHUB_PR_ATTACHMENTS."""
    assert attachments.SETTING_ENV_NAME == "GITHUB_PR_ATTACHMENTS"


def test_the_roots_variable_is_agent_proxy_roots() -> None:
    """The grant's roots come from AGENT_PROXY_ROOTS."""
    assert attachments.ROOTS_ENV_NAME == "AGENT_PROXY_ROOTS"


def test_attach_args_runs_with_the_setting_unset(root: Path) -> None:
    """With the setting unset, attach runs."""
    path = _file(root / "shot.png")

    result = attachments.attach_args([(path, None)], os.environ)

    assert result == [f"--attach={path}"]


def test_attach_args_runs_with_the_setting_on(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the setting on, attach runs."""
    monkeypatch.setenv("GITHUB_PR_ATTACHMENTS", "on")
    path = _file(root / "shot.png")

    result = attachments.attach_args([(path, None)], os.environ)

    assert result == [f"--attach={path}"]


def test_attach_args_refuses_with_the_setting_off(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the setting off, attach refuses."""
    monkeypatch.setenv("GITHUB_PR_ATTACHMENTS", "off")
    path = _file(root / "shot.png")

    assert _refusal([(path, None)]) == SETTING_OFF


def test_attach_args_refuses_another_setting_naming_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setting other than on or off is refused, and the reason names it."""
    monkeypatch.setenv("GITHUB_PR_ATTACHMENTS", "yes")
    path = _file(root / "shot.png")

    assert (
        _refusal([(path, None)])
        == "GITHUB_PR_ATTACHMENTS must be 'on' or 'off', got 'yes'"
    )


def test_the_setting_is_checked_before_the_roots(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An off setting is the refusal even when the roots are missing."""
    monkeypatch.setenv("GITHUB_PR_ATTACHMENTS", "off")
    monkeypatch.delenv("AGENT_PROXY_ROOTS")

    assert _refusal([(str(root / "shot.png"), None)]) == SETTING_OFF


def test_attach_args_refuses_without_the_roots(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without AGENT_PROXY_ROOTS, attach refuses."""
    monkeypatch.delenv("AGENT_PROXY_ROOTS")
    path = _file(root / "shot.png")

    assert _refusal([(path, None)]) == NO_ROOTS


@pytest.mark.parametrize(
    "value",
    ["not json", '"/a"', '["a"]', "[1]", "{}", "[" * 100_000],
    ids=["not-json", "string", "relative", "number", "object", "deep"],
)
def test_attach_args_refuses_roots_that_are_not_an_array_of_absolute_paths(
    root: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Roots that are not a JSON array of absolute paths are refused."""
    monkeypatch.setenv("AGENT_PROXY_ROOTS", value)
    path = _file(root / "shot.png")

    assert (
        _refusal([(path, None)])
        == f"AGENT_PROXY_ROOTS must be a JSON array of absolute paths, got '{value}'"
    )


def test_the_roots_are_checked_before_the_entries(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing roots are the refusal even when an entry is also bad."""
    monkeypatch.delenv("AGENT_PROXY_ROOTS")

    assert _refusal([(str(root / "missing.png"), None)]) == NO_ROOTS


def test_attach_args_refuses_a_file_outside_every_root(
    root: Path, tmp_path: Path
) -> None:
    """A file outside every root is refused."""
    path = _file(tmp_path / "outside.png")

    assert _refusal([(path, None)]) == _outside(path)


def test_attach_args_refuses_a_symlink_that_leads_out_of_the_root(
    root: Path, tmp_path: Path
) -> None:
    """A symlink inside a root that leads outside it is refused."""
    target = _file(tmp_path / "outside.png")
    link = root / "link.png"
    link.symlink_to(target)

    assert _refusal([(str(link), None)]) == _outside(str(link))


def test_attach_args_refuses_a_png_name_that_leads_to_an_env_file(root: Path) -> None:
    """A .png name is judged by the file it leads to, not by its name."""
    env_file = _file(root / ".env")
    link = root / "x.png"
    link.symlink_to(env_file)

    assert _refusal([(str(link), None)]) == _bad_extension(str(link))


def test_attach_args_refuses_a_name_without_an_accepted_extension_that_leads_to_a_png(
    root: Path,
) -> None:
    """A name without an accepted extension is refused even when it leads to a png."""
    target = _file(root / "shot.png")
    link = root / "x.txt"
    link.symlink_to(target)

    assert _refusal([(str(link), None)]) == _bad_extension(str(link))


def test_attach_args_compares_a_root_given_as_a_symlink_by_its_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root given as a symlink admits the files under its real path."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("AGENT_PROXY_ROOTS", json.dumps([str(link)]))
    monkeypatch.delenv("GITHUB_PR_ATTACHMENTS", raising=False)
    path = _file(real / "shot.png")

    result = attachments.attach_args([(path, None)], os.environ)

    assert result == [f"--attach={path}"]


def test_attach_args_refuses_a_path_holding_a_nul(root: Path) -> None:
    """A path holding a NUL byte is refused as outside the roots."""
    path = f"{root}/x\x00.png"

    assert _refusal([(path, None)]) == _outside(path)


def test_attach_args_refuses_a_directory(root: Path) -> None:
    """A directory is refused even when its name ends in .png."""
    directory = root / "x.png"
    directory.mkdir()

    assert _refusal([(str(directory), None)]) == _not_regular(str(directory))


def test_attach_args_refuses_a_missing_file(root: Path) -> None:
    """A path that names no file is refused."""
    path = str(root / "missing.png")

    assert _refusal([(path, None)]) == _not_regular(path)


def test_attach_args_refuses_a_text_file(root: Path) -> None:
    """A text file is refused for its extension."""
    path = _file(root / "notes.txt")

    assert _refusal([(path, None)]) == _bad_extension(path)


def test_attach_args_refuses_an_svg(root: Path) -> None:
    """An svg is refused for its extension."""
    path = _file(root / "logo.svg")

    assert _refusal([(path, None)]) == _bad_extension(path)


@pytest.mark.parametrize(
    "extension",
    ["png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "webm"],
)
def test_attach_args_accepts_each_extension(root: Path, extension: str) -> None:
    """Each accepted extension passes."""
    path = _file(root / f"shot.{extension}")

    result = attachments.attach_args([(path, None)], os.environ)

    assert result == [f"--attach={path}"]


def test_attach_args_compares_the_extension_in_any_case(root: Path) -> None:
    """An upper-case extension passes."""
    path = _file(root / "SHOT.PNG")

    result = attachments.attach_args([(path, None)], os.environ)

    assert result == [f"--attach={path}"]


def test_attach_args_passes_an_images_alt_after_a_hash(root: Path) -> None:
    """An image's alt follows the path after a hash."""
    path = _file(root / "shot.png")

    result = attachments.attach_args([(path, "The demo")], os.environ)

    assert result == [f"--attach={path}#The demo"]


def test_attach_args_refuses_a_video_with_alt(root: Path) -> None:
    """A video with an alt is refused."""
    path = _file(root / "demo.mp4")

    assert (
        _refusal([(path, "A demo")])
        == f"attach path '{path}' is a video, and a video takes no alt"
    )


def test_attach_args_returns_one_item_per_entry_in_order(root: Path) -> None:
    """The result holds one argv item per entry, in entry order."""
    mp4 = _file(root / "demo.mp4")
    png = _file(root / "shot.png")

    result = attachments.attach_args([(mp4, None), (png, "Shot")], os.environ)

    assert result == [f"--attach={mp4}", f"--attach={png}#Shot"]


def test_the_first_failing_entry_is_the_refusal(root: Path, tmp_path: Path) -> None:
    """When several entries fail, the first failing entry gives the reason."""
    good = _file(root / "shot.png")
    missing = str(root / "missing.png")
    outside = _file(tmp_path / "outside.png")

    assert _refusal([(good, None), (missing, None), (outside, None)]) == _not_regular(
        missing
    )
