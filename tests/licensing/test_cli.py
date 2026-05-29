"""Minimal smoke tests for the CLI `issue` subcommand.

Per test-strategy: licensing/cli.py is smoke-only, no unit coverage of argparse
internals. Verifies that main() with `issue` args exits 0, writes license.key,
and produces a v2 key.
"""

from pathlib import Path

from fis_monitor.licensing.cli import main


def test_issue_subcommand_creates_v2_key(tmp_path: Path) -> None:
    """issue --nbf --exp --out DIR → exit 0, license.key exists, starts with v2."""
    code = main(["issue", "--nbf", "2026-05-29", "--exp", "2026-12-31", "--out", str(tmp_path)])

    assert code == 0
    key_file = tmp_path / "license.key"
    assert key_file.exists(), "license.key must be created in --out directory"
    content = key_file.read_text(encoding="utf-8").strip()
    assert content.startswith("v2."), f"Key must start with 'v2.', got: {content[:20]!r}"


def test_issue_subcommand_exp_before_nbf_exits_1(tmp_path: Path) -> None:
    """issue with exp < nbf → exit 1, no file written."""
    code = main(["issue", "--nbf", "2026-12-31", "--exp", "2026-01-01", "--out", str(tmp_path)])

    assert code == 1
    assert not (tmp_path / "license.key").exists()


def test_issue_subcommand_out_is_file_not_dir_exits_1(tmp_path: Path) -> None:
    """--out pointing to a file (not a directory) → exit 1, no license.key written."""
    # Create a plain file where a directory is expected
    out_file = tmp_path / "not_a_dir.txt"
    out_file.write_text("i am a file\n", encoding="utf-8")

    code = main(["issue", "--nbf", "2026-05-29", "--exp", "2026-12-31", "--out", str(out_file)])

    assert code == 1
    # The file itself must not have been replaced by a directory entry
    assert out_file.is_file()
