"""CLI for generating licensing artifacts.

Two subcommands:
- init-secret: generates _P1/_P2 literals for _secret.py
- issue: issues a v1 license key

Installed as console script `gektar-gen-license` via [project.scripts] in
pyproject.toml. The module is shipped inside the wheel; end-user distributions
built with PyInstaller still bundle only the import graph of
`fis_monitor.app:main`, so this CLI is not reachable from a frozen release.
See ADR-057.
"""

import argparse
import base64
import secrets
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fis_monitor.licensing import _secret as _secret_module
from fis_monitor.licensing._codec import _canonical_bytes, encode_payload
from fis_monitor.licensing._hmac import sign
from fis_monitor.licensing._secret import _assemble_secret

_DURATION_DAYS: dict[str, int | None] = {
    "day": 1,
    "week": 7,
    "month": 30,
    "forever": None,
}


def _cmd_init_secret(args: argparse.Namespace) -> int:
    """Generate and print _P1/_P2 XOR-pair literals for _secret.py.

    Refuses to run when `_SECRET_INITIALIZED` is True (production secret
    already in place) unless `--force` is passed. Rotation invalidates
    every previously issued license key.
    """
    if getattr(_secret_module, "_SECRET_INITIALIZED", False) and not args.force:
        print(
            "ERROR: production secret is already initialized "
            "(_SECRET_INITIALIZED=True in fis_monitor/licensing/_secret.py).\n"
            "Re-running init-secret will rotate the secret and INVALIDATE "
            "every issued license key.\n"
            "If you intentionally want to rotate, pass --force.",
            file=sys.stderr,
        )
        return 1
    secret = secrets.token_bytes(32)
    p1 = secrets.token_bytes(32)
    p2 = bytes(a ^ b for a, b in zip(secret, p1, strict=True))
    print(f"_P1 = {p1!r}")
    print(f"_P2 = {p2!r}")
    return 0


def _cmd_issue(args: argparse.Namespace) -> int:
    """Issue a v1 license key and print or write it."""
    iat = datetime.now(UTC).date()

    if args.expires is not None:
        exp: date | None = args.expires
    else:
        days = _DURATION_DAYS[args.duration]
        exp = (iat + timedelta(days=days)) if days is not None else None

    if exp is not None and exp < iat:
        print(
            f"ERROR: --expires {exp.isoformat()} is before today ({iat.isoformat()}). "
            f"Refusing to issue a dead-on-arrival key.",
            file=sys.stderr,
        )
        return 1

    payload: dict[str, object] = {"v": 1, "iat": iat.isoformat(), "lic": args.licensee}
    if exp is not None:
        payload["exp"] = exp.isoformat()

    encoded_payload = encode_payload(payload)
    payload_bytes = _canonical_bytes(payload)
    secret = _assemble_secret()
    sig = sign(payload_bytes, secret)
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    key = f"v1.{encoded_payload}.{encoded_sig}"

    if args.out is not None:
        if args.out.is_dir():
            print(f"ERROR: --out {args.out} is a directory.", file=sys.stderr)
            return 1
        args.out.write_text(key + "\n", encoding="utf-8")
    else:
        print(key)

    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with init-secret and issue subcommands."""
    parser = argparse.ArgumentParser(
        prog="gektar-gen-license",
        description="Tool for generating licensing artifacts.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_p = sub.add_parser(
        "init-secret",
        help="Generate _P1/_P2 XOR-pair literals to paste into _secret.py.",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Allow rotation when _SECRET_INITIALIZED=True. "
            "WARNING: invalidates every previously issued license key."
        ),
    )
    init_p.set_defaults(func=_cmd_init_secret)

    issue_p = sub.add_parser("issue", help="Issue a v1 license key.")
    group = issue_p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--duration",
        choices=["day", "week", "month", "forever"],
        help="Relative expiry duration.",
    )
    group.add_argument(
        "--expires",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Absolute expiry date (ISO 8601).",
    )
    issue_p.add_argument("--licensee", required=True, help="Licensee name.")
    issue_p.add_argument(
        "--out",
        type=Path,
        metavar="FILE",
        help="Write key to this file instead of stdout.",
    )
    issue_p.set_defaults(func=_cmd_issue)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
