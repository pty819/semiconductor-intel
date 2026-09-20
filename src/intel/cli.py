"""Account CLI + worker/scheduler/api entry (spec 10 §2 / §4).

Account commands (``create-user`` / ``reset-password``) prompt for the
password twice via getpass and open their own async engine from
Settings.database_url. ``--help`` and argument errors parse offline.

Worker/scheduler/api commands do not prompt; they load Settings and run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from getpass import getpass

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
        prog="intel", description="semiconductor-intel CLI"
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
    worker = sub.add_parser(
        "worker", help="claim and run jobs (pipeline/fetch/research/all)"
    )
    worker.add_argument(
        "--role",
        default="all",
        help="worker role: pipeline, fetch, research, or all",
    )
    worker.add_argument("--idle-sleep", type=float, default=1.0)
    scheduler = sub.add_parser(
        "scheduler", help="enqueue due discover/report/watch jobs"
    )
    scheduler.add_argument("--sleep", type=float, default=30.0)
    api = sub.add_parser("api", help="run the FastAPI process (uvicorn)")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="alembic upgrade head")
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
    from sqlalchemy.ext.asyncio import create_async_engine

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


def _run_migrate() -> int:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    command.upgrade(cfg, "head")
    return 0


def _run_api(host: str, port: int) -> int:
    import uvicorn

    uvicorn.run("intel.api.app:app", host=host, port=port)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "worker":
        from intel.workers.main import worker_entry

        return worker_entry(["--role", args.role, "--idle-sleep", str(args.idle_sleep)])
    if args.command == "scheduler":
        from intel.workers.main import scheduler_entry

        return scheduler_entry(["--sleep", str(args.sleep)])
    if args.command == "api":
        return _run_api(args.host, args.port)
    if args.command == "migrate":
        return _run_migrate()
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
