from pathlib import Path


def resolve_base_dir(*, frozen: bool, executable: Path, module_file: Path) -> Path:
    """Distribution root that contains license.key.

    Args:
        frozen: True when running inside a PyInstaller --onedir bundle
            (i.e. ``getattr(sys, 'frozen', False)``).
        executable: ``Path(sys.executable)`` — used only when frozen.
        module_file: ``Path(__file__)`` of the calling module — used in
            src-layout (dev / test / non-frozen).

    Returns:
        Resolved base directory where ``license.key`` lives.

    Notes:
        frozen onedir layout::

            <root>/bin/fis-monitor        ← sys.executable
            <root>/bin/_internal/…        ← frozen modules
            <root>/license.key            ← expected location

        src-layout::

            <root>/src/fis_monitor/X.py   ← __file__
            <root>/license.key            ← expected location
    """
    if frozen:
        return executable.resolve().parent.parent
    return module_file.resolve().parent.parent.parent


def default_license_path(base_dir: Path) -> Path:
    """Return the canonical license.key path for a given base directory.

    Args:
        base_dir: Distribution root (as returned by :func:`resolve_base_dir`).

    Returns:
        ``base_dir / "license.key"``
    """
    return base_dir / "license.key"


def load_license_key(base_dir: Path) -> str:
    """Read the license key from ``<base_dir>/license.key``.

    Args:
        base_dir: Distribution root (as returned by :func:`resolve_base_dir`).

    Returns:
        Stripped key string (trailing newline / whitespace removed).

    Raises:
        FileNotFoundError: if ``license.key`` does not exist at the expected path.
    """
    return default_license_path(base_dir).read_text(encoding="utf-8").strip()
