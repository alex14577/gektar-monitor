from pathlib import Path


def _default_license_path(anchor: Path) -> Path:
    """Compute default license.key path: 3x .parent from anchor.

    For src-layout: src/fis_monitor/app.py -> project root / license.key.
    For PyInstaller --onedir: similarly resolves to package root.
    """
    return anchor.parent.parent.parent / "license.key"


def load_license_key(anchor: Path) -> str:
    """Read license key string from license.key next to the program.

    Args:
        anchor: Path to the calling module (__file__ resolved). Must be a
            resolved Path(__file__) of the caller (typically app.py); path is
            computed as 3× .parent relative to it (project root for
            src-layout).

    Returns:
        Stripped key string (trailing newline / whitespace removed).

    Raises:
        FileNotFoundError: if license.key does not exist at the computed path.
    """
    path = _default_license_path(anchor)
    return path.read_text(encoding="utf-8").strip()
