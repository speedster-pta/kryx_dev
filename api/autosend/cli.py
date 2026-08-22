"""Bootstrap CLI for creating User accounts.

Replaces the old create_superadmin.py and create_user.py scripts,
which duplicated password hashing, prompt routines, and DB setup with
minor (and drifting) differences. Both code paths now share one
password-hashing helper and one duplicate-username check, so the hash
format always matches exactly what authenticate_user() in admin_auth.py
expects at login.

Usage (inside the running container):
    docker compose exec kryx python -m autosend.cli superadmin <username>
    docker compose exec kryx python -m autosend.cli org <name> <slug>
    docker compose exec kryx python -m autosend.cli users <username>
    docker compose exec kryx python -m autosend.cli users <username> --org-admin
    docker compose exec kryx python -m autosend.cli users <username> --superadmin

Passwords are always prompted for interactively via getpass (never argv),
so they never land in shell history or `docker compose exec` process
listings.
"""
import argparse
import sys
from getpass import getpass

import bcrypt

from autosend import storage
from autosend.password_policy import validate_password_strength


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
    try:
        validate_password_strength(password)
    except ValueError as exc:
        print(str(exc))
        sys.exit(1)
    return password


def _ensure_username_available(username: str) -> None:
    if storage.get_user(username):
        print(f"A user '{username}' already exists - refusing to create a duplicate.")
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
        storage.assign_user_unit(user_id, unit["id"])
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
    user_id = storage.create_user(username, pw_hash, is_superadmin=True)
    print(f"Created superadmin '{username}' (id={user_id}). Log in at /login.")


def _prompt_org_id() -> int:
    slug = input("Organisation slug: ").strip()
    org = storage.get_organisation_by_slug(slug)
    if not org:
        print(f"No organisation found for slug '{slug}'. Create one first with: org <name> <slug>")
        sys.exit(1)
    return org.id


def cmd_org(args: argparse.Namespace) -> None:
    name = args.name.strip()
    slug = args.slug.strip()
    if not name or not slug:
        print("Both name and slug are required.")
        sys.exit(1)

    storage.init_db()
    if storage.get_organisation_by_slug(slug):
        print(f"An organisation with slug '{slug}' already exists - refusing to create a duplicate.")
        sys.exit(1)

    org = storage.create_organisation(name, slug)
    print(f"Created organisation '{org.name}' (id={org.id}, slug={org.slug}).")


def cmd_users(args: argparse.Namespace) -> None:
    username = args.username.strip()
    if not username:
        print("Username cannot be empty.")
        sys.exit(1)

    storage.init_db()
    _ensure_username_available(username)

    is_super = args.superadmin
    is_org_admin = args.org_admin
    org_id = None
    if not is_super:
        org_id = _prompt_org_id()

    password = _prompt_password("Password")
    pw_hash = _hash_password(password)
    user_id = storage.create_user(
        username, pw_hash, is_superadmin=is_super, org_id=org_id, is_org_admin=is_org_admin,
    )
    print(f"Created user {username} (id={user_id}, superadmin={is_super}, org_admin={is_org_admin})")

    if not is_super and not is_org_admin:
        _assign_units(user_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m autosend.cli",
        description="Bootstrap User accounts (superadmin or unit-scoped users).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_super = subparsers.add_parser("superadmin", help="Create a superadmin User.")
    p_super.add_argument("username")
    p_super.set_defaults(func=cmd_superadmin)

    p_org = subparsers.add_parser("org", help="Create an Organisation (superadmin-provisioned bootstrap path).")
    p_org.add_argument("name")
    p_org.add_argument("slug")
    p_org.set_defaults(func=cmd_org)

    p_users = subparsers.add_parser("users", help="Create a unit-scoped, org-admin, or superadmin User.")
    p_users.add_argument("username")
    p_users.add_argument(
        "--superadmin",
        action="store_true",
        help="Grant superadmin instead of prompting for an organisation.",
    )
    p_users.add_argument(
        "--org-admin",
        action="store_true",
        dest="org_admin",
        help="Grant org-admin (of the organisation you'll be prompted for) instead of prompting for unit slugs.",
    )
    p_users.set_defaults(func=cmd_users)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
