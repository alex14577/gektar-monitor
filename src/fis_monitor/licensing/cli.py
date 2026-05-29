"""CLI for generating licensing artifacts.

Two subcommands:
- init-secret: generates _P1/_P2 literals for _secret.py
- issue: issues a v2 license key

Interactive mode (no arguments): guided key generation via console prompts.

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
from datetime import date
from pathlib import Path

from fis_monitor.licensing import _secret as _secret_module
from fis_monitor.licensing._codec import _canonical_bytes, encode_payload
from fis_monitor.licensing._hmac import sign
from fis_monitor.licensing._interactive import _default_save_dir, run_interactive
from fis_monitor.licensing._prompt import ConsolePrompter, Prompter
from fis_monitor.licensing._secret import _assemble_secret


def _build_v2_key(nbf: date, exp: date, secret: bytes) -> str:
    """Build a v2 license key string.

    Pure function. No I/O, no side effects.

    Args:
        nbf: Not-before date (start of validity).
        exp: Expiry date (end of validity, inclusive).
        secret: 32-byte HMAC secret.

    Returns:
        License key string in ``v2.<payload>.<sig>`` format.
    """
    payload: dict[str, object] = {
        "v": 2,
        "nbf": nbf.isoformat(),
        "exp": exp.isoformat(),
        "lic": "interactive",
    }
    encoded_payload = encode_payload(payload)
    sig = sign(_canonical_bytes(payload), secret)
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"v2.{encoded_payload}.{encoded_sig}"


def _write_key(path: Path, key_str: str) -> None:
    """Write a key string to a file.

    Args:
        path: Destination file path.
        key_str: License key string to write.
    """
    path.write_text(key_str + "\n", encoding="utf-8")


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
    """Issue a v2 license key and write it to a file."""
    nbf: date = args.nbf
    exp: date = args.exp
    out_dir: Path = args.out

    if exp < nbf:
        print(
            f"ERROR: --exp {exp.isoformat()} is before --nbf ({nbf.isoformat()}). "
            f"Refusing to issue a key with inverted date range.",
            file=sys.stderr,
        )
        return 1

    if not out_dir.exists() or not out_dir.is_dir():
        print(
            f"ERROR: --out {out_dir} does not exist or is not a directory.",
            file=sys.stderr,
        )
        return 1

    secret = _assemble_secret()
    key = _build_v2_key(nbf, exp, secret)
    dest = out_dir / "license.key"

    if dest.is_symlink():
        print(
            f"ERROR: {dest} is a symlink. Refusing to write.",
            file=sys.stderr,
        )
        return 1

    try:
        _write_key(dest, key)
    except OSError as exc:
        print(f"ERROR: Could not write {dest}: {exc}", file=sys.stderr)
        return 1

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

    issue_p = sub.add_parser("issue", help="Issue a v2 license key.")
    issue_p.add_argument(
        "--nbf",
        required=True,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Not-before date (start of validity, ISO 8601).",
    )
    issue_p.add_argument(
        "--exp",
        required=True,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Expiry date (end of validity, ISO 8601).",
    )
    issue_p.add_argument(
        "--out",
        required=True,
        type=Path,
        metavar="DIR",
        help="Directory to write license.key into.",
    )
    issue_p.set_defaults(func=_cmd_issue)

    return parser


def _run_interactive_mode(prompter: Prompter) -> int:
    """Run interactive key generation mode.

    Args:
        prompter: Prompter implementation for I/O.

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    return run_interactive(
        prompter=prompter,
        key_writer=_write_key,
        builder=_build_v2_key,
        default_dir_fn=_default_save_dir,
        secret_fn=_assemble_secret,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI.

    When called with no arguments (interactive double-click mode), runs a
    guided key generation wizard. Otherwise, parses subcommands via argparse.

    Args:
        argv: Argument list. None means use sys.argv[1:].

    Returns:
        Exit code.
    """
    # Interactive mode: no argv given AND no real CLI arguments
    is_interactive = argv is None and len(sys.argv) == 1
    # Also support empty list for tests
    if argv is not None and len(argv) == 0:
        is_interactive = True

    if is_interactive:
        prompter = ConsolePrompter()
        try:
            code = _run_interactive_mode(prompter)
        except KeyboardInterrupt:
            print("\nОтменено.", file=sys.stderr)
            return 130
        except SystemExit:
            raise
        except Exception as exc:
            prompter.error(f"Неожиданная ошибка: {exc}")
            prompter.ask_text("Нажмите Enter для выхода…")
            return 1
        else:
            prompter.ask_text("Нажмите Enter для выхода…")
            return code

    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
