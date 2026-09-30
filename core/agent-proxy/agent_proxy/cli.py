"""The agent-proxy admin command: tokens, credentials, and the audit log."""

import argparse
import os
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from agent_proxy.audit import tail
from agent_proxy.auth import TokenStore
from agent_proxy.config import ConfigError, load_config, parse_duration
from agent_proxy.credentials import CredentialStore
from agent_proxy.errors import ProxyError

STATE_DIR_VARIABLE = "AGENT_PROXY_STATE_DIR"
CONFIG_VARIABLE = "AGENT_PROXY_CONFIG"
_COUNT_PATTERN = r"[1-9][0-9]*"


class SettingError(ProxyError):
    """An environment variable the command needs is unset or names no usable path."""


class UnknownScopeError(ProxyError):
    """A token was asked for a scope the config does not define."""


def run() -> None:
    """Run the admin command on the process arguments and exit with its status."""
    sys.exit(main())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the admin command on argv and return its exit status.

    A refusal prints one stderr line per failure and returns 1. A usage error
    exits 2 inside argparse.
    """
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except ConfigError as error:
        _report_refusal(error.errors)
        return 1
    except ProxyError as error:
        _report_refusal([str(error)])
        return 1
    return 0


def _report_refusal(messages: Sequence[str]) -> None:
    for message in messages:
        print(f"agent-proxy: {message}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-proxy",
        description="Administer the tokens, credentials, and audit log of one"
        " proxy instance.",
    )
    nouns = parser.add_subparsers(dest="noun", required=True)
    _add_token_commands(
        nouns.add_parser("token", help="mint, list, and revoke bearer tokens")
    )
    _add_credential_commands(
        nouns.add_parser("credential", help="store, list, and remove credentials")
    )
    _add_audit_commands(nouns.add_parser("audit", help="read the audit log"))
    return parser


def _add_token_commands(parser: argparse.ArgumentParser) -> None:
    verbs = parser.add_subparsers(dest="verb", required=True)
    mint = verbs.add_parser("mint", help="mint a token and print it")
    mint.add_argument("--name", required=True)
    mint.add_argument("--scope", required=True)
    mint.add_argument(
        "--expires",
        metavar="DURATION",
        help="lifetime such as 12h or 30d; without it the token never expires",
    )
    mint.set_defaults(handler=_mint_token)
    listing = verbs.add_parser("list", help="list tokens without their secrets")
    listing.set_defaults(handler=_list_tokens)
    revoke = verbs.add_parser("revoke", help="delete a token")
    revoke.add_argument("name", metavar="NAME")
    revoke.set_defaults(handler=_revoke_token)


def _add_credential_commands(parser: argparse.ArgumentParser) -> None:
    verbs = parser.add_subparsers(dest="verb", required=True)
    store = verbs.add_parser("set", help="store the value read from stdin")
    store.add_argument("name", metavar="NAME")
    store.set_defaults(handler=_set_credential)
    listing = verbs.add_parser("list", help="list credential names and set times")
    listing.set_defaults(handler=_list_credentials)
    remove = verbs.add_parser("rm", help="delete a credential")
    remove.add_argument("name", metavar="NAME")
    remove.set_defaults(handler=_remove_credential)


def _add_audit_commands(parser: argparse.ArgumentParser) -> None:
    verbs = parser.add_subparsers(dest="verb", required=True)
    tail_parser = verbs.add_parser("tail", help="print the newest audit lines")
    tail_parser.add_argument(
        "-n",
        dest="count",
        metavar="COUNT",
        type=_positive_count,
        default=10,
        help="how many lines to print (default 10)",
    )
    tail_parser.set_defaults(handler=_tail_audit)


def _positive_count(text: str) -> int:
    if re.fullmatch(_COUNT_PATTERN, text) is None:
        raise argparse.ArgumentTypeError(
            f"COUNT must be a positive integer, got {text!r}"
        )
    return int(text)


def _mint_token(args: argparse.Namespace) -> None:
    expires_in = None if args.expires is None else parse_duration(args.expires)
    config = load_config(_config_path(), _credential_store().names())
    if args.scope not in config.scopes:
        raise UnknownScopeError(
            f"scope {args.scope!r} is not defined in the config;"
            f" defined scopes are {sorted(config.scopes)!r}"
        )
    print(_token_store().mint(args.name, args.scope, expires_in))


def _list_tokens(args: argparse.Namespace) -> None:
    records = _token_store().records()
    print(_row("NAME", "SCOPE", "CREATED", "LAST_USED", "EXPIRES"))
    for record in records:
        print(
            _row(
                record.name,
                record.scope,
                _format_time(record.created_at),
                _format_time(record.last_used_at),
                _format_time(record.expires_at),
            )
        )


def _revoke_token(args: argparse.Namespace) -> None:
    _token_store().revoke(args.name)
    print(f"revoked {args.name}")


def _set_credential(args: argparse.Namespace) -> None:
    store = _credential_store()
    value = sys.stdin.buffer.read()
    store.set(args.name, value)
    print(f"stored {args.name} ({len(value)} bytes)")


def _list_credentials(args: argparse.Namespace) -> None:
    entries = _credential_store().entries()
    print(_row("NAME", "SET_AT"))
    for entry in entries:
        print(_row(entry.name, _format_time(entry.set_at)))


def _remove_credential(args: argparse.Namespace) -> None:
    _credential_store().remove(args.name)
    print(f"removed {args.name}")


def _tail_audit(args: argparse.Namespace) -> None:
    for line in tail(_state_dir() / "audit", args.count):
        print(line)


def _state_dir() -> Path:
    value = os.environ.get(STATE_DIR_VARIABLE)
    if not value or not Path(value).is_dir():
        raise SettingError(
            f"{STATE_DIR_VARIABLE} must name an existing directory, got {value!r}"
        )
    return Path(value)


def _config_path() -> Path:
    value = os.environ.get(CONFIG_VARIABLE)
    if not value:
        raise SettingError(
            f"{CONFIG_VARIABLE} must name the config file, got {value!r}"
        )
    return Path(value)


def _token_store() -> TokenStore:
    return TokenStore(_state_dir() / "tokens.sqlite3")


def _credential_store() -> CredentialStore:
    return CredentialStore(_state_dir() / "credentials")


def _row(*fields: str) -> str:
    return "\t".join(fields)


def _format_time(moment: datetime | None) -> str:
    return "-" if moment is None else moment.isoformat(timespec="seconds")
