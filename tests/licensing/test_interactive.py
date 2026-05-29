"""Tests for _interactive.py — application logic layer.

Uses RecordingPrompter fake (DI seam) to test all invariants without any I/O.
ConsolePrompter (IO adapter) is NOT tested here per test strategy.
"""

import sys
from collections import deque
from datetime import date
from pathlib import Path

import pytest

from fis_monitor.licensing._interactive import _default_save_dir, run_interactive
from fis_monitor.licensing._prompt import Prompter
from tests.licensing.conftest import _TEST_SECRET, make_v2_key

# ---------------------------------------------------------------------------
# RecordingPrompter fake
# ---------------------------------------------------------------------------


class RecordingPrompter:
    """Fake Prompter that consumes pre-programmed answers and records all calls."""

    def __init__(self, answers: list[str], yes_no_answers: list[bool] | None = None) -> None:
        self._answers: deque[str] = deque(answers)
        self._yes_no_answers: deque[bool] = deque(yes_no_answers or [])
        self.asked: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []

    def ask_text(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self._answers:
            raise RuntimeError(f"RecordingPrompter ran out of answers at prompt: {prompt!r}")
        return self._answers.popleft()

    def ask_yes_no(self, prompt: str) -> bool:
        self.asked.append(prompt)
        if not self._yes_no_answers:
            raise RuntimeError(
                f"RecordingPrompter ran out of yes/no answers at prompt: {prompt!r}"
            )
        return self._yes_no_answers.popleft()

    def info(self, message: str) -> None:
        self.infos.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


# ---------------------------------------------------------------------------
# Fake builder and writer helpers
# ---------------------------------------------------------------------------


def _fake_builder(nbf: date, exp: date, secret: bytes) -> str:
    return make_v2_key(nbf, exp, secret)


def _make_writer(log: list[tuple[Path, str]]) -> object:
    def writer(path: Path, key_str: str) -> None:
        log.append((path, key_str))
    return writer


# ---------------------------------------------------------------------------
# Anti-fake: ensure every Prompter method is exercised
# ---------------------------------------------------------------------------


def test_recording_prompter_satisfies_protocol() -> None:
    """RecordingPrompter implements every method of the Prompter Protocol."""
    p = RecordingPrompter(answers=["hello"], yes_no_answers=[True])
    assert isinstance(p, Prompter)
    assert p.ask_text("q?") == "hello"
    assert p.ask_yes_no("yn?") is True
    p.info("msg")
    p.error("err")
    assert "q?" in p.asked
    assert "yn?" in p.asked
    assert "msg" in p.infos
    assert "err" in p.errors


# ---------------------------------------------------------------------------
# Invariant: valid happy path
# ---------------------------------------------------------------------------


def test_run_interactive_happy_path(tmp_path: Path) -> None:
    """Valid inputs → key_writer called with correct path and v2 key, returns 0."""
    prompter = RecordingPrompter(
        answers=[
            "2026-01-01",         # nbf
            "2026-12-31",         # exp
            str(tmp_path),        # directory
        ]
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 0
    assert len(written) == 1
    path, key_str = written[0]
    assert path == tmp_path / "license.key"
    assert key_str.startswith("v2.")
    assert len(prompter.infos) >= 1
    assert str(tmp_path / "license.key") in prompter.infos[0]


# ---------------------------------------------------------------------------
# Invariant: exp < nbf → error shown, exp re-asked
# ---------------------------------------------------------------------------


def test_run_interactive_exp_before_nbf_retries(tmp_path: Path) -> None:
    """exp < nbf triggers error and retry; loop continues until valid exp."""
    prompter = RecordingPrompter(
        answers=[
            "2026-06-01",   # nbf
            "2026-01-01",   # exp: invalid (before nbf)
            "2026-12-31",   # exp: valid
            str(tmp_path),  # directory
        ]
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 0
    assert len(written) == 1
    assert len(prompter.errors) >= 1
    # nbf asked once, exp asked twice (first invalid, then valid), dir asked once
    exp_asks = [
        q for q in prompter.asked
        if "exp" in q.lower() and "сохранения" not in q
    ]
    assert len(exp_asks) == 2


# ---------------------------------------------------------------------------
# Invariant: invalid nbf date → error + retry nbf
# ---------------------------------------------------------------------------


def test_run_interactive_invalid_nbf_retries(tmp_path: Path) -> None:
    """Malformed nbf string triggers error and re-asks nbf."""
    prompter = RecordingPrompter(
        answers=[
            "not-a-date",   # nbf: invalid
            "2026-01-01",   # nbf: valid
            "2026-12-31",   # exp
            str(tmp_path),  # directory
        ]
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 0
    assert len(prompter.errors) >= 1
    # Verify nbf was asked twice
    nbf_asks = [q for q in prompter.asked if "nbf" in q.lower() or "начала" in q.lower()]
    assert len(nbf_asks) == 2


# ---------------------------------------------------------------------------
# Invariant: non-existent directory → error + retry
# ---------------------------------------------------------------------------


def test_run_interactive_nonexistent_directory_retries(tmp_path: Path) -> None:
    """Non-existent directory path triggers error and retry."""
    bad_dir = tmp_path / "does_not_exist"
    prompter = RecordingPrompter(
        answers=[
            "2026-01-01",   # nbf
            "2026-12-31",   # exp
            str(bad_dir),   # directory: does not exist
            str(tmp_path),  # directory: valid
        ]
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 0
    assert any("не найдена" in e or str(bad_dir) in e for e in prompter.errors)


# ---------------------------------------------------------------------------
# Invariant: file exists + ask_yes_no=False → retry directory
# ---------------------------------------------------------------------------


def test_run_interactive_file_exists_no_overwrite_retries(tmp_path: Path) -> None:
    """Existing license.key + overwrite=False triggers directory retry."""
    # Pre-create the file
    existing = tmp_path / "license.key"
    existing.write_text("old key\n", encoding="utf-8")

    other_dir = tmp_path / "subdir"
    other_dir.mkdir()

    prompter = RecordingPrompter(
        answers=[
            "2026-01-01",   # nbf
            "2026-12-31",   # exp
            str(tmp_path),  # dir: has license.key
            str(other_dir), # dir: no conflict
        ],
        yes_no_answers=[False],  # don't overwrite
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 0
    assert written[0][0] == other_dir / "license.key"


# ---------------------------------------------------------------------------
# Invariant: file exists + ask_yes_no=True → overwrite (writer called)
# ---------------------------------------------------------------------------


def test_run_interactive_file_exists_yes_overwrite(tmp_path: Path) -> None:
    """Existing license.key + overwrite=True → writer called with same path."""
    existing = tmp_path / "license.key"
    existing.write_text("old key\n", encoding="utf-8")

    prompter = RecordingPrompter(
        answers=[
            "2026-01-01",  # nbf
            "2026-12-31",  # exp
            str(tmp_path), # dir
        ],
        yes_no_answers=[True],  # overwrite
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 0
    assert written[0][0] == tmp_path / "license.key"


# ---------------------------------------------------------------------------
# Invariant: key_writer raises OSError → error shown, returns 1
# ---------------------------------------------------------------------------


def test_run_interactive_os_error_returns_1(tmp_path: Path) -> None:
    """OSError from key_writer → error message shown and return code 1."""
    prompter = RecordingPrompter(
        answers=[
            "2026-01-01",  # nbf
            "2026-12-31",  # exp
            str(tmp_path), # directory
        ]
    )

    def failing_writer(path: Path, key_str: str) -> None:
        raise OSError("disk full")

    code = run_interactive(
        prompter=prompter,
        key_writer=failing_writer,
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
    )

    assert code == 1
    assert any("disk full" in e or "записать" in e for e in prompter.errors)


# ---------------------------------------------------------------------------
# Invariant: max_retries exhaustion → return 1, writer never called
# ---------------------------------------------------------------------------


def test_run_interactive_max_retries_nbf_exhausted(tmp_path: Path) -> None:
    """10 consecutive invalid nbf inputs → return 1, writer never called."""
    prompter = RecordingPrompter(
        answers=["bad-date"] * 10,
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
        max_retries=10,
    )

    assert code == 1
    assert len(written) == 0
    assert len(prompter.errors) >= 1


def test_run_interactive_max_retries_exp_exhausted(tmp_path: Path) -> None:
    """Valid nbf then 10 consecutive invalid exp inputs → return 1, writer never called."""
    prompter = RecordingPrompter(
        answers=["2026-01-01"] + ["bad-date"] * 10,
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
        max_retries=10,
    )

    assert code == 1
    assert len(written) == 0
    assert len(prompter.errors) >= 1


def test_run_interactive_max_retries_dir_exhausted(tmp_path: Path) -> None:
    """Valid nbf/exp then 10 consecutive bad directories → return 1, writer never called."""
    bad_dir = tmp_path / "nonexistent"
    prompter = RecordingPrompter(
        answers=["2026-01-01", "2026-12-31"] + [str(bad_dir)] * 10,
    )
    written: list[tuple[Path, str]] = []

    code = run_interactive(
        prompter=prompter,
        key_writer=_make_writer(written),
        builder=_fake_builder,
        default_dir_fn=lambda: tmp_path,
        secret_fn=lambda: _TEST_SECRET,
        max_retries=10,
    )

    assert code == 1
    assert len(written) == 0
    assert len(prompter.errors) >= 1


# ---------------------------------------------------------------------------
# _default_save_dir branching
# ---------------------------------------------------------------------------


def test_default_save_dir_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Frozen=True → returns directory of sys.executable."""
    fake_exe = tmp_path / "some_dir" / "app.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.touch()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    result = _default_save_dir()
    assert result == fake_exe.parent


def test_default_save_dir_not_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frozen attribute absent/False → returns Path.cwd()."""
    monkeypatch.delattr(sys, "frozen", raising=False)

    result = _default_save_dir()
    assert result == Path.cwd()
