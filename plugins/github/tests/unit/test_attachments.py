import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from github_plugin import attachments
from github_plugin.scripts import RefusalError

ACCEPTED = "png, jpg, jpeg, gif, webp, mp4, mov, or webm"
SETTING_OFF = "attach is off: GITHUB_PR_ATTACHMENTS is 'off'"
NO_ROOTS = "attach needs the grant's roots: AGENT_PROXY_ROOTS is not set"
CAGE_TREE_PARENT = attachments.TREE_PARENT


@pytest.fixture(autouse=True)
def tree_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep each private tree in tmp_path/trees, which starts empty."""
    parent = tmp_path / "trees"
    parent.mkdir()
    monkeypatch.setattr(attachments, "TREE_PARENT", str(parent))
    return parent


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


def _private_path(item: str) -> str:
    """Return the path an --attach= item gives gh, without any alt."""
    return item.removeprefix("--attach=").partition("#")[0]


def _agent_items(attached: attachments.AttachedFiles) -> list[str]:
    """Return attached's --attach= items, each with its path normalized."""
    items = []
    for item in attached.args:
        _, hash_mark, alt = item.partition("#")
        items.append(
            f"--attach={os.path.normpath(_private_path(item))}{hash_mark}{alt}"
        )
    return items


def _swap_before_open(
    monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]
) -> None:
    """Make swap run after the realpath check, just before the script opens the file."""
    real_open = os.open

    def swap_then_open(path: str, flags: int) -> int:
        swap()
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", swap_then_open)


def test_the_private_tree_lives_on_the_cages_tmpfs() -> None:
    """The tree's parent is /tmp, the one writable tmpfs of a cage."""
    assert CAGE_TREE_PARENT == "/tmp"  # noqa: S108 - the path the cage mounts


def test_the_tree_parent_does_not_follow_tmpdir(
    root: Path, tree_parent: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TMPDIR from the grant's env does not move the tree off the parent."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("TMPDIR", str(elsewhere))
    path = _file(root / "shot.png")

    attachments.attach_args([(path, None)], os.environ)

    assert list(elsewhere.iterdir()) == []
    assert len(list(tree_parent.iterdir())) == 1


def test_the_path_given_to_gh_is_not_the_agents_and_normalizes_to_it(
    root: Path,
) -> None:
    """The private path cleans lexically to the path a body reference names."""
    (root / "sub").mkdir()
    path = _file(root / "sub" / "shot.png")

    attached = attachments.attach_args([(path, None)], os.environ)

    private = _private_path(attached.args[0])
    assert private != path
    assert os.path.normpath(private) == path


def test_a_path_with_extra_spelling_normalizes_like_the_agents(root: Path) -> None:
    """An entry's redundant parts normalize away on both sides."""
    (root / "sub").mkdir()
    _file(root / "shot.png")
    spelled = f"{root}/./sub/../shot.png"

    attached = attachments.attach_args([(spelled, None)], os.environ)

    private = _private_path(attached.args[0])
    assert os.path.normpath(private) == os.path.normpath(spelled)
    assert Path(private).read_bytes() == b"x"


def test_a_path_with_two_leading_slashes_reads_the_file(root: Path) -> None:
    """Two leading slashes survive normpath, and the private path holds one."""
    _file(root / "shot.png")

    attached = attachments.attach_args([(f"/{root}/shot.png", None)], os.environ)

    private = _private_path(attached.args[0])
    assert os.path.normpath(private) == str(root / "shot.png")
    assert Path(private).read_bytes() == b"x"


def test_the_private_path_reads_the_checked_files_bytes(root: Path) -> None:
    """Reading the private path yields the file's bytes."""
    shot = root / "shot.png"
    shot.write_bytes(b"checked bytes")

    attached = attachments.attach_args([(str(shot), None)], os.environ)

    assert Path(_private_path(attached.args[0])).read_bytes() == b"checked bytes"


def test_the_private_path_is_a_regular_file_to_stat(root: Path) -> None:
    """A stat follows the private path to the file's own type, size, and inode."""
    shot = root / "shot.png"
    shot.write_bytes(b"checked bytes")

    attached = attachments.attach_args([(str(shot), None)], os.environ)

    private = Path(_private_path(attached.args[0]))
    assert private.is_file()
    assert private.stat().st_size == len(b"checked bytes")
    assert private.stat().st_ino == shot.stat().st_ino


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda shot, _: shot.unlink(), id="deleted"),
        pytest.param(
            lambda shot, _: shot.rename(shot.with_name("n.png")), id="renamed"
        ),
        pytest.param(
            lambda shot, secret: (shot.unlink(), shot.symlink_to(secret)),
            id="replaced-by-a-symlink",
        ),
    ],
)
def test_the_private_path_keeps_the_checked_bytes_after_the_agents_name_changes(
    root: Path, tmp_path: Path, change: Callable[[Path, Path], object]
) -> None:
    """A later change to the agent's name does not change what gh's path reads."""
    shot = root / "shot.png"
    shot.write_bytes(b"checked bytes")
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret bytes")

    attached = attachments.attach_args([(str(shot), None)], os.environ)
    change(shot, secret)

    assert Path(_private_path(attached.args[0])).read_bytes() == b"checked bytes"


def test_each_entry_reads_its_own_bytes_when_entries_share_parents(root: Path) -> None:
    """Entries below one directory share the tree's parents and keep their own links."""
    first = root / "first.png"
    second = root / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    attached = attachments.attach_args(
        [(str(first), None), (str(second), None)], os.environ
    )

    assert [Path(_private_path(item)).read_bytes() for item in attached.args] == [
        b"first",
        b"second",
    ]


def test_a_path_that_shares_the_trees_own_directory_names_still_reads(
    root: Path,
) -> None:
    """A root holding a directory named up lands in the tree's up chain safely."""
    (root / "up" / "up").mkdir(parents=True)
    path = _file(root / "up" / "up" / "shot.png")

    attached = attachments.attach_args([(path, None)], os.environ)

    private = _private_path(attached.args[0])
    assert os.path.normpath(private) == path
    assert Path(private).read_bytes() == b"x"


def test_the_same_path_twice_gives_the_same_private_path(root: Path) -> None:
    """Both items name one private path, so gh's own same-file refusal still fires."""
    path = _file(root / "shot.png")

    attached = attachments.attach_args([(path, None), (path, None)], os.environ)

    assert attached.args[0] == attached.args[1]


def test_a_swap_to_a_symlink_out_of_the_roots_before_open_is_refused(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check on the opened file sees a swap that lands after the realpath check."""
    secret = _file(tmp_path / "secret.png")
    shot = root / "shot.png"
    _file(shot)

    def swap() -> None:
        shot.unlink()
        shot.symlink_to(secret)

    _swap_before_open(monkeypatch, swap)

    assert _refusal([(str(shot), None)]) == _outside(str(shot))


def test_a_swap_to_a_symlink_to_an_env_file_before_open_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extension check reads the opened file's own name."""
    env_file = _file(root / ".env")
    shot = root / "shot.png"
    _file(shot)

    def swap() -> None:
        shot.unlink()
        shot.symlink_to(env_file)

    _swap_before_open(monkeypatch, swap)

    assert _refusal([(str(shot), None)]) == _bad_extension(str(shot))


def test_a_file_deleted_before_open_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file gone between the realpath check and the open is a refusal."""
    shot = root / "shot.png"
    _file(shot)
    _swap_before_open(monkeypatch, shot.unlink)

    assert _refusal([(str(shot), None)]) == _not_regular(str(shot))


def test_attach_args_refuses_a_fifo_without_hanging(root: Path) -> None:
    """A FIFO with an accepted name is refused, and opening it does not block."""
    fifo = root / "x.png"
    os.mkfifo(fifo)

    assert _refusal([(str(fifo), None)]) == _not_regular(str(fifo))


def test_attach_args_refuses_a_path_through_a_file_that_realpath_passes(
    root: Path,
) -> None:
    """A realpath cleans x.png/.. away, and the kernel will not open through a file."""
    _file(root / "x.png")
    _file(root / "y.png")
    path = f"{root}/x.png/../y.png"

    assert _refusal([(path, None)]) == _not_regular(path)


def test_a_refused_call_leaves_no_tree(root: Path, tree_parent: Path) -> None:
    """The tree is built only after every entry passes."""
    good = _file(root / "shot.png")
    missing = str(root / "missing.png")

    assert _refusal([(good, None), (missing, None)]) == _not_regular(missing)
    assert list(tree_parent.iterdir()) == []


@pytest.mark.parametrize("file_first", [True, False], ids=["file-first", "file-last"])
def test_entries_that_use_one_name_as_a_file_and_a_directory_are_refused(
    root: Path, file_first: bool
) -> None:
    """Two entries that need one name to be a file and a directory are a refusal.

    Each passes its own checks: the first normalizes to <root>/x.png but opens
    <root>/sub/x.png through the symlink, and the second lies in the directory
    <root>/x.png.
    """
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "link").symlink_to(root / "sub" / "deep")
    _file(root / "sub" / "x.png")
    (root / "x.png").mkdir()
    in_directory = _file(root / "x.png" / "y.png")
    through_link = f"{root}/link/../x.png"
    entries: list[tuple[str, str | None]] = [(through_link, None), (in_directory, None)]
    if not file_first:
        entries.reverse()

    assert _refusal(entries) == _not_regular(entries[1][0])


def test_restore_replaces_each_private_path_with_the_entrys_path(root: Path) -> None:
    """The text comes back naming each entry's path as the agent spelled it."""
    (root / "sub").mkdir()
    first = _file(root / "first.png")
    _file(root / "second.png")
    second = f"{root}/sub/../second.png"
    attached = attachments.attach_args([(first, None), (second, None)], os.environ)
    first_private, second_private = map(_private_path, attached.args)

    restored = attached.restore(f"{first_private} and {second_private}: refused\n")

    assert restored == f"{first} and {second}: refused\n"


def test_restore_replaces_a_longer_private_path_whole(root: Path) -> None:
    """A private path that begins with another one is replaced as itself."""
    (root / "sub").mkdir()
    _file(root / "x.png")
    _file(root / "x.png.png")
    short = f"{root}/./x.png"
    long = f"{root}/sub/../x.png.png"
    attached = attachments.attach_args([(short, None), (long, None)], os.environ)

    restored = attached.restore(_private_path(attached.args[1]))

    assert restored == long


def test_restore_leaves_a_text_without_a_private_path_as_it_is(root: Path) -> None:
    """A text that names no private path comes back unchanged."""
    attached = attachments.attach_args([(_file(root / "x.png"), None)], os.environ)

    assert attached.restore("https://github.com/o/n/pull/1\n") == (
        "https://github.com/o/n/pull/1\n"
    )


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

    assert _agent_items(result) == [f"--attach={path}"]


def test_attach_args_runs_with_the_setting_on(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the setting on, attach runs."""
    monkeypatch.setenv("GITHUB_PR_ATTACHMENTS", "on")
    path = _file(root / "shot.png")

    result = attachments.attach_args([(path, None)], os.environ)

    assert _agent_items(result) == [f"--attach={path}"]


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

    assert _agent_items(result) == [f"--attach={path}"]


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

    assert _agent_items(result) == [f"--attach={path}"]


def test_attach_args_compares_the_extension_in_any_case(root: Path) -> None:
    """An upper-case extension passes."""
    path = _file(root / "SHOT.PNG")

    result = attachments.attach_args([(path, None)], os.environ)

    assert _agent_items(result) == [f"--attach={path}"]


def test_attach_args_passes_an_images_alt_after_a_hash(root: Path) -> None:
    """An image's alt follows the path after a hash."""
    path = _file(root / "shot.png")

    result = attachments.attach_args([(path, "The demo")], os.environ)

    assert _agent_items(result) == [f"--attach={path}#The demo"]


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

    assert _agent_items(result) == [f"--attach={mp4}", f"--attach={png}#Shot"]


def test_the_first_failing_entry_is_the_refusal(root: Path, tmp_path: Path) -> None:
    """When several entries fail, the first failing entry gives the reason."""
    good = _file(root / "shot.png")
    missing = str(root / "missing.png")
    outside = _file(tmp_path / "outside.png")

    assert _refusal([(good, None), (missing, None), (outside, None)]) == _not_regular(
        missing
    )
