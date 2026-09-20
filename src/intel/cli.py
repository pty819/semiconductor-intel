"""Account CLI (spec 10 §2 账号 CLI 建立): ``uv run intel create-user`` /
``uv run intel reset-password``.

Prompts for the password twice via getpass (min length enforced, never
echoed, never logged). Opens its own async engine from
Settings.database_url — this is the bootstrap path for the first account,
so it must work with no server running. ``--help`` and argument errors
parse offline: no engine is created until a command actually runs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from getpass import getpass

from sqlalchemy.ext.asyncio import create_async_engine

from intel.services.identity import (
    MIN_PASSWORD_LENGTH,
    IdentityService,
    InvalidLogin,
    LoginTaken,
    SqlAlchemyIdentityRepository,
    UnknownLogin,
    WeakPassword,
)
from intel.settings import Settings


class PasswordPromptError(ValueError):
    """The entered password is too short or the two entries differ."""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="intel", description="semiconductor-intel account CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser(
        "create-user", help="create a login account (prompts for a password)"
    )
    create.add_argument("login")
    reset = sub.add_parser(
        "reset-password",
        help="set a new password and revoke all of the login's sessions",
    )
    reset.add_argument("login")
    return parser.parse_args(argv)


def _prompt_password() -> str:
    first = getpass("Password: ")
    second = getpass("Confirm password: ")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise PasswordPromptError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if first != second:
        raise PasswordPromptError("passwords do not match")
    return first


async def _run_command(command: str, login: str, password: str) -> int:
    settings = Settings()
    service = IdentityService.from_settings(settings)
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            repo = SqlAlchemyIdentityRepository(conn)
            if command == "create-user":
                view = await service.create_user(repo, login, password)
                print(f"created user {view.login} (id={view.id})")
            elif command == "reset-password":
                await service.reset_password(repo, login, password)
                print(f"password updated for {login}; all sessions revoked")
            else:  # pragma: no cover - argparse restricts the choices
                raise ValueError(f"unknown command {command!r}")
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        password = _prompt_password()
    except PasswordPromptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_run_command(args.command, args.login, password))
    except (InvalidLogin, LoginTaken, UnknownLogin, WeakPassword) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
