"""Interactive bootstrap command for the first app-owned administrator."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from averon_import.services.account_auth import AccountRepository, UserAlreadyExists


def _default_data_dir() -> Path:
    configured = os.environ.get("AVERON_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (root / "Averon Import" / "data").resolve()
    return (Path(__file__).resolve().parents[1] / "data").resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m averon_import.auth_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-admin", help="Create the app-owned administrator account")
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--data-dir", type=Path, default=None)
    bootstrap.add_argument(
        "--reset-existing",
        action="store_true",
        help="Explicitly replace the named account password and enable it as ADMIN",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    password = getpass.getpass("Новый пароль администратора: ")
    confirmation = getpass.getpass("Повторите пароль администратора: ")
    if password != confirmation:
        print("Пароли не совпадают.", file=sys.stderr)
        return 2
    data_dir = args.data_dir or _default_data_dir()
    repository = AccountRepository(data_dir / "auth")
    try:
        user = repository.bootstrap_admin(
            args.username,
            password,
            replace_existing=args.reset_existing,
        )
    except UserAlreadyExists:
        print("Эта учётная запись уже существует. Для явного сброса используйте --reset-existing.", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Администратор {user['username']} создан." if user["version"] == 1 else f"Администратор {user['username']} обновлён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
