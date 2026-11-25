"""The instance scripts refuse bad callers and act in the agent's tree as the agent."""

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).parents[3] / "docker"

TREE_COMMAND = re.compile(r"(?<![\w-])(?:chgrp|chmod|find|install|rm|setfacl)(?![\w-])")

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="the root refusal is only observable as a non-root user"
)


def run_script(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the script name from core/docker with arguments, capturing its output."""
    return subprocess.run(  # noqa: S603 - the tests pass their own arguments
        [str(DOCKER_DIR / name), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def run_sourced(
    script: str, body: str, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Source script under bash, then run body with env added, capturing its output.

    The `_` takes `$0`, so `BASH_SOURCE[0]` differs from it and the script's guard
    keeps `main` from running. The timeout fails a run that never ends: a tree check
    that lets a tree outside the home through walks its parents forever.
    """
    return subprocess.run(  # noqa: S603 - the tests pass their own body
        ["/bin/bash", "-c", f'source "$1"; {body}', "_", str(DOCKER_DIR / script)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
        timeout=10,
    )


def recording_stub(command: str) -> str:
    """Return a shell function named command that appends its call to `$STUB_LOG`."""
    return f'{command}() {{ echo "{command} $*" >> "$STUB_LOG"; }}; '


def stub_calls(stub_log: Path) -> list[str]:
    """Return the calls the recording stubs appended to stub_log, one per line."""
    if not stub_log.exists():
        return []
    return stub_log.read_text().splitlines()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("provision-instance.sh", ("agent-01", "8100", "/nonexistent")),
        (
            "provision-instance.sh",
            ("--release", "0.1.0", "--tree", "/nonexistent", "agent-01", "8100")
            + ("/nonexistent",),
        ),
        ("teardown-instance.sh", ("agent-01",)),
        ("teardown-instance.sh", ("--tree", "/nonexistent", "agent-01")),
    ],
)
def test_script_refuses_a_non_root_user(name: str, arguments: tuple[str, ...]) -> None:
    """A full command line from a non-root user exits 1 before any step runs."""
    result = run_script(name, *arguments)

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == f"{name}: must run as root\n"


PROVISION_USAGE = (
    "usage: provision-instance.sh [--release VERSION] [--tree PATH] "
    "<agent> <port> <config-file>"
)
TEARDOWN_USAGE = "usage: teardown-instance.sh [--tree PATH] <agent>"
BUILD_IMAGES_USAGE = "usage: build-images.sh <version> <registry> <source-url>"


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("provision-instance.sh", PROVISION_USAGE),
        ("teardown-instance.sh", TEARDOWN_USAGE),
        ("build-images.sh", BUILD_IMAGES_USAGE),
    ],
)
def test_script_without_arguments_prints_its_usage(name: str, usage: str) -> None:
    """No arguments exits 2 with the usage line on stderr."""
    result = run_script(name)

    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"{usage}\n"


def test_build_images_needs_all_three_arguments() -> None:
    """Two of the three arguments exit 2 with the usage line, before any build."""
    result = run_script("build-images.sh", "0.1.0", "r")

    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"{BUILD_IMAGES_USAGE}\n"


@pytest.mark.parametrize("version", ["v0.1.0", "0.1", "0.1.0-rc.1", "0.1.0\n"])
def test_build_images_refuses_a_version_that_is_not_bare(version: str) -> None:
    """A version that is not <major>.<minor>.<patch> exits 1 before any build."""
    result = run_script("build-images.sh", version, "r", "s")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "build-images.sh: the version must be <major>.<minor>.<patch>, "
        f"got '{version}'\n"
    )


@pytest.mark.parametrize(
    ("name", "arguments", "usage"),
    [
        (
            "provision-instance.sh",
            ("--bogus", "agent-01", "8100", "/nonexistent"),
            PROVISION_USAGE,
        ),
        ("teardown-instance.sh", ("--bogus", "agent-01"), TEARDOWN_USAGE),
    ],
)
def test_script_with_an_unknown_option_prints_its_usage_before_the_root_check(
    name: str, arguments: tuple[str, ...], usage: str
) -> None:
    """An unknown option from a non-root user exits 2 with the usage line, not 1."""
    result = run_script(name, *arguments)

    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"{usage}\n"


@pytest.mark.parametrize(
    ("script", "arguments", "expected"),
    [
        (
            "provision-instance.sh",
            "--release 0.1.0 --tree /t agent-01 8100 /c",
            {"release_version": "0.1.0", "tree_option": "/t"},
        ),
        (
            "provision-instance.sh",
            "--tree /t --release 0.1.0 -- agent-01 8100 /c",
            {"release_version": "0.1.0", "tree_option": "/t"},
        ),
        (
            "provision-instance.sh",
            "agent-01 8100 /c",
            {"release_version": "", "tree_option": ""},
        ),
        ("teardown-instance.sh", "--tree /t agent-01", {"tree_option": "/t"}),
        ("teardown-instance.sh", "-- agent-01", {"tree_option": ""}),
        ("teardown-instance.sh", "agent-01", {"tree_option": ""}),
    ],
)
def test_parse_arguments_sets_the_options_and_the_positional_arguments(
    script: str, arguments: str, expected: dict[str, str]
) -> None:
    """The options before the positional arguments set their globals; absent, empty."""
    positional = (
        {"agent": "agent-01", "port": "8100", "config_file": "/c"}
        if script == "provision-instance.sh"
        else {"agent": "agent-01"}
    )
    names = " ".join([*expected, *positional])

    result = run_sourced(script, f"parse_arguments {arguments}; declare -p {names}", {})

    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.splitlines() == [
        f'declare -- {name}="{value}"'
        for name, value in {**expected, **positional}.items()
    ]


@pytest.mark.parametrize(
    ("script", "arguments", "usage"),
    [
        ("provision-instance.sh", "--bogus agent-01 8100 /c", PROVISION_USAGE),
        ("provision-instance.sh", "--release", PROVISION_USAGE),
        ("provision-instance.sh", "--tree", PROVISION_USAGE),
        ("provision-instance.sh", '--release "" agent-01 8100 /c', PROVISION_USAGE),
        ("provision-instance.sh", '--tree "" agent-01 8100 /c', PROVISION_USAGE),
        ("provision-instance.sh", "--release 0.1.0 agent-01 8100", PROVISION_USAGE),
        ("teardown-instance.sh", "--bogus agent-01", TEARDOWN_USAGE),
        ("teardown-instance.sh", "--tree", TEARDOWN_USAGE),
        ("teardown-instance.sh", '--tree "" agent-01', TEARDOWN_USAGE),
        ("teardown-instance.sh", "--tree /t", TEARDOWN_USAGE),
    ],
)
def test_parse_arguments_refuses_a_bad_command_line_with_the_usage(
    script: str, arguments: str, usage: str
) -> None:
    """An unknown option, an option with no value, or too few arguments exits 2."""
    result = run_sourced(script, f"parse_arguments {arguments}; echo parsed", {})

    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"{usage}\n"


@pytest.mark.parametrize(
    ("release_version", "calls", "report"),
    [
        (
            "0.1.0",
            [
                "service_docker pull ghcr.io/gravital-forge/agent-proxy:0.1.0",
                "service_docker tag ghcr.io/gravital-forge/agent-proxy:0.1.0 "
                "agent-proxy:1",
            ],
            "pulled ghcr.io/gravital-forge/agent-proxy:0.1.0 as agent-proxy:1 "
            "on the daemon of agentproxy-agent-01",
        ),
        (
            "",
            [
                "service_docker build --quiet --tag agent-proxy-verifier:1 "
                "/s/../verifier",
                "service_docker build --quiet --tag agent-proxy:1 --file /s/Dockerfile "
                "/s/../agent-proxy",
            ],
            "built agent-proxy-verifier:1 and agent-proxy:1 "
            "on the daemon of agentproxy-agent-01",
        ),
    ],
)
def test_prepare_images_pulls_a_release_or_builds_from_the_checkout(
    tmp_path: Path, release_version: str, calls: list[str], report: str
) -> None:
    """Release mode pulls and retags the instance image; source mode builds both."""
    stub_log = tmp_path / "stub.log"
    body = (
        recording_stub("service_docker")
        + 'script_dir=/s service_user=agentproxy-agent-01 release_version="$RELEASE"; '
        + "prepare_images"
    )

    result = run_sourced(
        "provision-instance.sh",
        body,
        {"STUB_LOG": str(stub_log), "RELEASE": release_version},
    )

    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == f"{report}\n"
    assert stub_calls(stub_log) == calls


def test_print_next_steps_points_at_the_deployment_guide() -> None:
    """The last next step names the deployment guide, which exists in the checkout."""
    guide = DOCKER_DIR.resolve().parents[1] / "docs" / "deployment.md"
    body = (
        'script_dir="$SCRIPT_DIR" docker_host=unix:///run/user/1001/docker.sock; '
        "print_next_steps"
    )

    result = run_sourced(
        "provision-instance.sh", body, {"SCRIPT_DIR": str(DOCKER_DIR.resolve())}
    )

    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.splitlines()[-1] == (
        f"next, follow the deployment guide: {guide}"
    )
    assert guide.is_file()


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Return an agent home under tmp_path holding Projects and work/repos."""
    home = tmp_path.resolve() / "home"
    (home / "Projects").mkdir(parents=True)
    (home / "work" / "repos").mkdir(parents=True)
    return home


PASSWD_STUB = (
    'getent() { case "$2" in '
    'agent-01) echo "agent-01:x:1000:1000::$AGENT_HOME:/bin/bash" ;; '
    "agentproxy-agent-01) "
    'echo "agentproxy-agent-01:x:1001:1001::/svc:/usr/sbin/nologin" ;; '
    "*) return 2 ;; esac; }; "
)


def run_check_arguments(
    agent_home: Path, tree_option: str, then_run: str, release_version: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run provisioning's check_arguments for agent-01, then the shell text then_run."""
    config_file = agent_home.parent / "config.yaml"
    config_file.write_text("agent_tree: x\n")
    body = (
        PASSWD_STUB
        + recording_stub("as_agent")
        + "id() { echo agents; }; "
        + 'agent=agent-01 port=8100 config_file="$CONFIG_FILE" '
        + "service_user=agentproxy-agent-01 "
        + 'release_version="$RELEASE" tree_option="$TREE_OPTION"; '
        + f"check_arguments; {then_run}"
    )
    return run_sourced(
        "provision-instance.sh",
        body,
        {
            "AGENT_HOME": str(agent_home),
            "CONFIG_FILE": str(config_file),
            "RELEASE": release_version,
            "TREE_OPTION": tree_option,
            "STUB_LOG": str(agent_home.parent / "stub.log"),
        },
    )


@pytest.mark.parametrize(
    ("tree", "expected"), [("work/repos", "work/repos"), ("", "Projects")]
)
def test_check_arguments_takes_the_tree_option_or_the_default(
    agent_home: Path, tree: str, expected: str
) -> None:
    """--tree names the agent's tree; without it the tree is <home>/Projects."""
    tree_option = str(agent_home / tree) if tree else ""

    result = run_check_arguments(agent_home, tree_option, "declare -p agent_tree")

    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.splitlines()[-1] == (
        f'declare -- agent_tree="{agent_home / expected}"'
    )


def test_check_arguments_refuses_a_tree_outside_the_home(agent_home: Path) -> None:
    """A tree whose realpath is outside the home's realpath exits 1."""
    elsewhere = agent_home.parent / "elsewhere"
    elsewhere.mkdir()

    result = run_check_arguments(agent_home, str(elsewhere), "echo checked")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"provision-instance.sh: the agent's tree '{elsewhere}' must lie inside "
        f"the agent's home '{agent_home}'\n"
    )


def test_check_arguments_refuses_the_home_itself_as_the_tree(agent_home: Path) -> None:
    """A tree equal to the home exits 1, so no ACL lands on the whole home."""
    result = run_check_arguments(agent_home, str(agent_home), "echo checked")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"provision-instance.sh: the agent's tree '{agent_home}' must lie inside "
        f"the agent's home '{agent_home}'\n"
    )


def test_check_arguments_refuses_a_default_tree_that_resolves_outside_the_home(
    agent_home: Path,
) -> None:
    """A <home>/Projects symbolic link to a directory outside the home exits 1."""
    elsewhere = agent_home.parent / "elsewhere"
    elsewhere.mkdir()
    (agent_home / "Projects").rmdir()
    (agent_home / "Projects").symlink_to(elsewhere)

    result = run_check_arguments(agent_home, "", "echo checked")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"provision-instance.sh: the agent's tree '{elsewhere}' must lie inside "
        f"the agent's home '{agent_home}'\n"
    )


def test_check_arguments_refuses_a_tree_that_is_not_a_directory(
    agent_home: Path,
) -> None:
    """A --tree naming no directory exits 1 with the path as given."""
    missing = agent_home / "missing"

    result = run_check_arguments(agent_home, str(missing), "echo checked")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"provision-instance.sh: the agent's tree '{missing}' is not a directory\n"
    )


def test_check_arguments_refuses_a_release_that_is_not_a_bare_version(
    agent_home: Path,
) -> None:
    """--release v0.1.0 exits 1: the version carries no v."""
    result = run_check_arguments(agent_home, "", "echo checked", "v0.1.0")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "provision-instance.sh: the release must be <major>.<minor>.<patch>, "
        "got 'v0.1.0'\n"
    )


@pytest.mark.parametrize(
    ("tree", "calls"),
    [
        (
            "work/repos",
            ["as_agent setfacl -m u:agentproxy-agent-01:x {home}/work"],
        ),
        ("Projects", []),
    ],
)
def test_grant_tree_traverse_grants_each_directory_between_the_home_and_the_tree(
    agent_home: Path, tree: str, calls: list[str]
) -> None:
    """The agent grants the service user traverse strictly between home and tree."""
    result = run_check_arguments(
        agent_home, str(agent_home / tree), "grant_tree_traverse"
    )

    assert (result.returncode, result.stderr) == (0, "")
    assert stub_calls(agent_home.parent / "stub.log") == [
        call.format(home=agent_home) for call in calls
    ]
    assert result.stdout.splitlines()[-1] == (
        "granted agentproxy-agent-01 traverse as agent-01 on each directory "
        f"between {agent_home} and {agent_home / tree}, {len(calls)} in all"
    )


def run_teardown_acls(
    agent_home: Path, tree_option: str
) -> subprocess.CompletedProcess[str]:
    """Run teardown's check_agent and remove_acls for agent-01 with setfacl stubbed."""
    body = (
        PASSWD_STUB
        + recording_stub("setfacl")
        + "agent=agent-01 service_user=agentproxy-agent-01 "
        + 'tree_option="$TREE_OPTION" agent_tree="" service_uid="" service_home="" '
        + 'docker_host="" tree_parents_in_home=(); '
        + "check_agent >/dev/null; remove_acls"
    )
    return run_sourced(
        "teardown-instance.sh",
        body,
        {
            "AGENT_HOME": str(agent_home),
            "TREE_OPTION": tree_option,
            "STUB_LOG": str(agent_home.parent / "stub.log"),
        },
    )


def test_remove_acls_clears_the_tree_the_directories_above_it_and_the_home(
    agent_home: Path,
) -> None:
    """Teardown removes the service user's entries up to and including the home."""
    tree = agent_home / "work" / "repos"

    result = run_teardown_acls(agent_home, str(tree))

    assert (result.returncode, result.stderr) == (0, "")
    assert stub_calls(agent_home.parent / "stub.log") == [
        f"setfacl -R -P -x u:agentproxy-agent-01 {tree}",
        f"setfacl -R -P -x d:u:agentproxy-agent-01 {tree}",
        f"setfacl -x u:agentproxy-agent-01 {agent_home / 'work'}",
        f"setfacl -x u:agentproxy-agent-01 {agent_home}",
    ]
    assert result.stdout == (
        f"removed the ACL entries of agentproxy-agent-01 from the agent's tree {tree} "
        f"and from {agent_home / 'work'} {agent_home}\n"
    )


def test_remove_acls_clears_the_home_alone_when_the_tree_is_gone(
    agent_home: Path,
) -> None:
    """With no tree, teardown knows no directory above it and clears the home."""
    result = run_teardown_acls(agent_home, str(agent_home / "gone" / "repos"))

    assert (result.returncode, result.stderr) == (0, "")
    assert stub_calls(agent_home.parent / "stub.log") == [
        f"setfacl -x u:agentproxy-agent-01 {agent_home}",
    ]
    assert result.stdout == (
        f"removed the ACL entries of agentproxy-agent-01 from {agent_home}\n"
    )


def test_check_agent_refuses_a_tree_outside_the_home(agent_home: Path) -> None:
    """Teardown refuses a tree outside the home before it removes any ACL entry."""
    elsewhere = agent_home.parent / "elsewhere"
    elsewhere.mkdir()

    result = run_teardown_acls(agent_home, str(elsewhere))

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"teardown-instance.sh: the agent's tree '{elsewhere}' must lie inside "
        f"the agent's home '{agent_home}'\n"
    )
    assert stub_calls(agent_home.parent / "stub.log") == []


def test_check_agent_refuses_the_home_itself_as_the_tree(agent_home: Path) -> None:
    """A tree equal to the home exits 1, so no recursive removal walks the home."""
    result = run_teardown_acls(agent_home, str(agent_home))

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"teardown-instance.sh: the agent's tree '{agent_home}' must lie inside "
        f"the agent's home '{agent_home}'\n"
    )
    assert stub_calls(agent_home.parent / "stub.log") == []


def test_check_agent_refuses_a_default_tree_that_resolves_outside_the_home(
    agent_home: Path,
) -> None:
    """A <home>/Projects symbolic link to a directory outside the home exits 1."""
    elsewhere = agent_home.parent / "elsewhere"
    elsewhere.mkdir()
    (agent_home / "Projects").rmdir()
    (agent_home / "Projects").symlink_to(elsewhere)

    result = run_teardown_acls(agent_home, "")

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"teardown-instance.sh: the agent's tree '{elsewhere}' must lie inside "
        f"the agent's home '{agent_home}'\n"
    )
    assert stub_calls(agent_home.parent / "stub.log") == []


def function_body(script: str, function: str) -> str:
    """Return the body of the shell function named function in script."""
    text = (DOCKER_DIR / script).read_text()
    pattern = rf"^{function}\(\) \{{\n(.*?)^\}}"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    assert match, f"{script} defines no function {function}"
    return match.group(1)


@pytest.mark.parametrize("script", ["provision-instance.sh", "teardown-instance.sh"])
def test_as_agent_runs_as_the_agent_from_the_root_directory(script: str) -> None:
    """The helper drops to the agent through runuser, outside root's cwd."""
    expected = '(cd / && runuser -u "$agent" -- "$@")'
    assert function_body(script, "as_agent").strip() == expected


def test_both_scripts_share_one_tree_check() -> None:
    """Teardown's recursive ACL removal sits behind the check provisioning applies."""
    assert function_body(
        "teardown-instance.sh", "find_tree_parents_in_home"
    ) == function_body("provision-instance.sh", "find_tree_parents_in_home")


@pytest.mark.parametrize(
    ("script", "function"),
    [
        ("provision-instance.sh", "create_overflow_dir"),
        ("provision-instance.sh", "apply_tree_permissions"),
        ("provision-instance.sh", "grant_tree_traverse"),
        ("teardown-instance.sh", "remove_overflow_dir"),
    ],
)
def test_step_changes_the_agent_tree_only_as_the_agent(
    script: str, function: str
) -> None:
    """Every command that walks or writes the agent's tree runs as the agent."""
    body = function_body(script, function)

    unguarded = [
        match.group()
        for match in TREE_COMMAND.finditer(body)
        if not body[: match.start()].endswith(("as_agent ", "-exec "))
    ]

    assert TREE_COMMAND.search(body)
    assert unguarded == []


HOME = "/home/agent-01"
AGENT_PASSWD = "agent-01:x:1000:1000::/home/agent-01:/bin/bash"
SERVICE_PASSWD = "agentproxy-agent-01:x:1001:1001::/svc:/usr/sbin/nologin"
AGENT_GROUP = "agent-01:x:1000:"

HOME_STUBS = (
    'getfacl() { printf "%s" "$ACL"; }; '
    'stat() { echo "$HOME_GROUP"; }; '
    'getent() { case "$1" in '
    'group) [[ "$2" == "$HOME_GROUP" && -n "$GROUP_ENTRY" ]] '
    '&& echo "$GROUP_ENTRY" ;; '
    'passwd) printf "%s\\n" "$PASSWD" ;; esac; }; '
)


def run_warn_open_home(
    tmp_path: Path,
    acl: list[str],
    group_entry: str = AGENT_GROUP,
    passwd: tuple[str, ...] = (AGENT_PASSWD, SERVICE_PASSWD),
) -> subprocess.CompletedProcess[str]:
    """Run provisioning's warn_open_home on a home whose getfacl -pc prints acl.

    The home's owning group is agent-01, and getent answers group_entry for it and
    passwd for the user list. Recording stubs catch any chmod or setfacl call.
    """
    body = (
        HOME_STUBS
        + recording_stub("chmod")
        + recording_stub("setfacl")
        + f"agent=agent-01 service_user=agentproxy-agent-01 agent_home={HOME}; "
        + "warn_open_home; echo next step"
    )
    return run_sourced(
        "provision-instance.sh",
        body,
        {
            "ACL": "".join(f"{line}\n" for line in [*acl, ""]),
            "HOME_GROUP": "agent-01",
            "GROUP_ENTRY": group_entry,
            "PASSWD": "\n".join(passwd),
            "STUB_LOG": str(tmp_path / "stub.log"),
        },
    )


def open_home_warning(who: str, command: str) -> str:
    """Return the line warn_open_home prints on stderr for who, closed by command."""
    return (
        f"provision-instance.sh: warning: {who} can traverse {HOME}; "
        f"only agent-01 and agentproxy-agent-01 may; close it with '{command}'"
    )


CLOSED_HOME_REPORT = (
    f"checked that only agent-01 and agentproxy-agent-01 can traverse {HOME}"
)
SERVICE_ENTRY = "user:agentproxy-agent-01:--x"


@pytest.mark.parametrize(
    ("acl", "group_entry", "passwd"),
    [
        pytest.param(
            ["user::rwx", SERVICE_ENTRY, "group::---", "mask::--x", "other::---"],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            id="mode 700",
        ),
        pytest.param(
            ["user::rwx", SERVICE_ENTRY, "group::r-x", "mask::r-x", "other::---"],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            id="mode 750, the group has no other member",
        ),
        pytest.param(
            [
                "user::rwx",
                SERVICE_ENTRY,
                "group::---",
                "group:staff:r-x\t#effective:r--",
                "mask::r--",
                "other::---",
            ],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            id="a named group whose x the mask removes",
        ),
        pytest.param(
            [
                "user::rwx",
                SERVICE_ENTRY,
                "user:agent-01:rwx",
                "group::---",
                "mask::rwx",
                "other::---",
            ],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            id="a named entry for the agent",
        ),
        pytest.param(
            [
                "user::rwx",
                SERVICE_ENTRY,
                "group::---",
                "mask::--x",
                "other::---",
                "default:user::rwx",
                "default:user:bob:r-x",
                "default:group::r-x",
                "default:group:staff:r-x",
                "default:mask::r-x",
                "default:other::r-x",
            ],
            "agent-01:x:1000:bob",
            (AGENT_PASSWD, SERVICE_PASSWD),
            id="open default entries",
        ),
    ],
)
def test_warn_open_home_reports_a_closed_home(
    tmp_path: Path, acl: list[str], group_entry: str, passwd: tuple[str, ...]
) -> None:
    """A home only the agent and the service user can traverse gets no warning."""
    result = run_warn_open_home(tmp_path, acl, group_entry, passwd)

    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == f"{CLOSED_HOME_REPORT}\nnext step\n"
    assert stub_calls(tmp_path / "stub.log") == []


@pytest.mark.parametrize(
    ("acl", "group_entry", "passwd", "who", "command"),
    [
        pytest.param(
            ["user::rwx", SERVICE_ENTRY, "group::r-x", "mask::r-x", "other::---"],
            "agent-01:x:1000:agent-01,bob",
            (AGENT_PASSWD, SERVICE_PASSWD),
            "owning group agent-01",
            f"setfacl -m g::r-- {HOME}",
            id="mode 750, the group lists another member",
        ),
        pytest.param(
            ["user::rwx", SERVICE_ENTRY, "group::r-x", "mask::r-x", "other::---"],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD, "carol:x:1002:1000::/home/carol:/bin/sh"),
            "owning group agent-01",
            f"setfacl -m g::r-- {HOME}",
            id="mode 750, the group is another user's primary group",
        ),
        pytest.param(
            ["user::rwx", SERVICE_ENTRY, "group::r-x", "mask::r-x", "other::---"],
            "",
            (AGENT_PASSWD, SERVICE_PASSWD),
            "owning group agent-01",
            f"setfacl -m g::r-- {HOME}",
            id="mode 750, getent knows no such group",
        ),
        pytest.param(
            ["user::rwx", SERVICE_ENTRY, "group::---", "mask::--x", "other::--x"],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            "any other user",
            f"chmod o-x {HOME}",
            id="an open other",
        ),
        pytest.param(
            [
                "user::rwx",
                SERVICE_ENTRY,
                "user:bob:r-x",
                "group::---",
                "mask::r-x",
                "other::---",
            ],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            "user bob",
            f"setfacl -x u:bob {HOME}",
            id="a named user",
        ),
        pytest.param(
            [
                "user::rwx",
                SERVICE_ENTRY,
                "group::---",
                "group:staff:r-x",
                "mask::r-x",
                "other::---",
            ],
            AGENT_GROUP,
            (AGENT_PASSWD, SERVICE_PASSWD),
            "group staff",
            f"setfacl -x g:staff {HOME}",
            id="a named group",
        ),
    ],
)
def test_warn_open_home_warns_for_each_entry_that_can_traverse(
    tmp_path: Path,
    acl: list[str],
    group_entry: str,
    passwd: tuple[str, ...],
    who: str,
    command: str,
) -> None:
    """An entry that lets someone else traverse warns on stderr and changes nothing."""
    result = run_warn_open_home(tmp_path, acl, group_entry, passwd)

    assert result.returncode == 0
    assert result.stderr == f"{open_home_warning(who, command)}\n"
    assert result.stdout == "next step\n"
    assert stub_calls(tmp_path / "stub.log") == []


def test_warn_open_home_warns_once_for_each_open_entry(tmp_path: Path) -> None:
    """Two open entries give two warnings, in the order getfacl lists them."""
    acl = [
        "user::rwx",
        SERVICE_ENTRY,
        "user:bob:--x",
        "group::---",
        "mask::--x",
        "other::r-x",
    ]

    result = run_warn_open_home(tmp_path, acl)

    assert (result.returncode, result.stdout) == (0, "next step\n")
    assert result.stderr.splitlines() == [
        open_home_warning("user bob", f"setfacl -x u:bob {HOME}"),
        open_home_warning("any other user", f"chmod o-x {HOME}"),
    ]


def test_provisioning_checks_the_home_right_after_it_grants_traverse() -> None:
    """Provisioning runs warn_open_home as the step after grant_home_traverse."""
    assert "    grant_home_traverse\n    warn_open_home\n" in function_body(
        "provision-instance.sh", "main"
    )
