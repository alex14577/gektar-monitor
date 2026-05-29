"""Interactive key generation flow for the CLI.

Pure application logic — all I/O injected via Prompter and callables.
No input()/print() calls, no sys.exit(), no KeyboardInterrupt handling.
"""

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from fis_monitor.licensing._prompt import Prompter


def _default_save_dir() -> Path:
    """Return the default directory for saving the license key.

    In a frozen (PyInstaller) executable: directory of sys.executable.
    Otherwise (dev / installed wheel): current working directory.

    Returns:
        Path to the default save directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


def run_interactive(
    prompter: Prompter,
    key_writer: Callable[[Path, str], None],
    builder: Callable[[date, date, bytes], str],
    default_dir_fn: Callable[[], Path],
    secret_fn: Callable[[], bytes],
    max_retries: int = 10,
) -> int:
    """Run the interactive key generation workflow.

    Asks for nbf, exp, and output directory interactively, then builds and
    writes the license key.

    Designed to be fully pure-testable via injected callables and Prompter.
    Does NOT call sys.exit() or handle KeyboardInterrupt (caller's responsibility).

    Args:
        prompter: I/O interface for asking questions and showing messages.
        key_writer: Callable(path, key_str) — writes the key to disk.
        builder: Callable(nbf, exp, secret) -> key_str — builds the key.
        default_dir_fn: Returns the default save directory when user input is empty.
        secret_fn: Returns the HMAC secret bytes.
        max_retries: Maximum number of invalid input attempts per question before
            aborting. Protects against broken/piped stdin. Default is 10.

    Returns:
        0 on success, 1 on error (too many retries, symlink conflict, or OSError).
    """
    # --- Ask for nbf with retry on invalid date ---
    nbf: date | None = None
    nbf_attempts = 0
    while nbf is None:
        if nbf_attempts >= max_retries:
            prompter.error("Слишком много неудачных попыток.")
            return 1
        raw_nbf = prompter.ask_text("Дата начала действия (nbf, YYYY-MM-DD): ").strip()
        try:
            nbf = date.fromisoformat(raw_nbf)
        except ValueError:
            prompter.error(f"Неверный формат даты: «{raw_nbf}». Ожидается YYYY-MM-DD.")
            nbf_attempts += 1

    # --- Ask for exp with retry on invalid date or exp < nbf ---
    exp: date | None = None
    exp_attempts = 0
    while exp is None:
        if exp_attempts >= max_retries:
            prompter.error("Слишком много неудачных попыток.")
            return 1
        raw_exp = prompter.ask_text("Дата окончания действия (exp, YYYY-MM-DD): ").strip()
        try:
            candidate = date.fromisoformat(raw_exp)
        except ValueError:
            prompter.error(f"Неверный формат даты: «{raw_exp}». Ожидается YYYY-MM-DD.")
            exp_attempts += 1
            continue
        if candidate < nbf:
            prompter.error(
                f"Дата окончания ({candidate}) не может быть раньше даты начала ({nbf})."
            )
            exp_attempts += 1
            continue
        exp = candidate

    # --- Ask for output directory with retry on non-existent/non-dir path ---
    dir_attempts = 0
    while True:
        if dir_attempts >= max_retries:
            prompter.error("Слишком много неудачных попыток.")
            return 1
        default_dir = default_dir_fn()
        raw_dir = prompter.ask_text(
            f"Директория для сохранения [Enter = {default_dir}]: "
        ).strip()
        target_dir = default_dir if raw_dir == "" else Path(raw_dir)

        if not target_dir.exists() or not target_dir.is_dir():
            prompter.error(f"Директория не найдена: «{target_dir}».")
            dir_attempts += 1
            continue

        dest_path = target_dir / "license.key"

        if dest_path.is_symlink():
            prompter.error(f"Файл «{dest_path}» является символической ссылкой. Отказ.")
            dir_attempts += 1
            continue

        if dest_path.exists():
            overwrite = prompter.ask_yes_no(
                f"Файл «{dest_path}» существует. Перезаписать? [y/N]: "
            )
            if not overwrite:
                dir_attempts += 1
                continue

        break

    # --- Build and write the key ---
    key_str = builder(nbf, exp, secret_fn())

    try:
        key_writer(dest_path, key_str)
    except OSError as exc:
        prompter.error(f"Не удалось записать файл: {exc}")
        return 1

    prompter.info(f"Ключ сохранён: {dest_path}")
    return 0
