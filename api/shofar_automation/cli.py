"""Bootstrap CLI for creating StaffUser accounts.

Replaces the old create_superadmin.py and create_staff_user.py scripts,
which duplicated password hashing, prompt routines, and DB setup with
minor (and drifting) differences. Both code paths now share one
password-hashing helper and one duplicate-username check, so the hash
format always matches exactly what authenticate_staff_user() in admin.py
expects at login.

Usage (inside the running container):
    docker compose exec whatsapp-manager python -m shofar_automation.cli superadmin <username>
    docker compose exec whatsapp-manager python -m shofar_automation.cli staff <username>
    docker compose exec whatsapp-manager python -m shofar_automation.cli staff <username> --superadmin

Passwords are always prompted for interactively via getpass (never argv),
so they never land in shell history or `docker compose exec` process
listings.
"""
import argparse
import sys
from getpass import getpass

import bcrypt

from shofar_automation import storage


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _prompt_password(label: str = "Password") -> str:
    password = getpass(f"{label}: ")
    confirm = getpass("Confirm password: ")
    if not password:
        print("Password cannot be empty.")
        sys.exit(1)
    if password != confirm:
        print("Passwords did not match.")
        sys.exit(1)
    return password


def _ensure_username_available(username: str) -> None:
    if storage.get_staff_user(username):
        print(f"A staff user '{username}' already exists - refusing to create a duplicate.")
        sys.exit(1)


def _assign_units(user_id: int) -> None:
    raw = input("Unit slugs (comma-separated): ").strip()
    for slug in raw.split(","):
        slug = slug.strip()
        if not slug:
            continue
        unit = storage.get_unit_by_slug(slug)
        if not unit:
            print(f"  WARNING: no unit found for slug '{slug}', skipping")
            continue
        storage.assign_staff_unit(user_id, unit["id"])
        print(f"  Granted access to {slug}")


def cmd_superadmin(args: argparse.Namespace) -> None:
    username = args.username.strip()
    if not username:
        print("Username cannot be empty.")
        sys.exit(1)

    storage.init_db()
    _ensure_username_available(username)

    password = _prompt_password("Password for new superadmin")
    pw_hash = _hash_password(password)
    user_id = storage.create_staff_user(username, pw_hash, is_superadmin=True)
    print(f"Created superadmin '{username}' (id={user_id}). Log in at /login.")


def cmd_staff(args: argparse.Namespace) -> None:
    username = args.username.strip()
    if not username:
        print("Username cannot be empty.")
        sys.exit(1)

    storage.init_db()
    _ensure_username_available(username)

    is_super = args.superadmin
    password = _prompt_password("Password")
    pw_hash = _hash_password(password)
    user_id = storage.create_staff_user(username, pw_hash, is_superadmin=is_super)
    print(f"Created user {username} (id={user_id}, superadmin={is_super})")

    if not is_super:
        _assign_units(user_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m shofar_automation.cli",
        description="Bootstrap StaffUser accounts (superadmin or unit-scoped staff).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_super = subparsers.add_parser("superadmin", help="Create a superadmin StaffUser.")
    p_super.add_argument("username")
    p_super.set_defaults(func=cmd_superadmin)

    p_staff = subparsers.add_parser("staff", help="Create a unit-scoped (or superadmin) StaffUser.")
    p_staff.add_argument("username")
    p_staff.add_argument(
        "--superadmin",
        action="store_true",
        help="Grant superadmin instead of prompting for unit slugs.",
    )
    p_staff.set_defaults(func=cmd_staff)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
