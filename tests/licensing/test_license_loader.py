from pathlib import Path

import pytest

from fis_monitor._license_loader import default_license_path, load_license_key, resolve_base_dir


def test_load_license_key_valid(tmp_path):
    (tmp_path / "license.key").write_text("v2.payload.sig", encoding="utf-8")

    result = load_license_key(tmp_path)

    assert result == "v2.payload.sig"


def test_load_license_key_missing_raises(tmp_path):
    # no license.key written intentionally
    with pytest.raises(FileNotFoundError):
        load_license_key(tmp_path)


def test_load_license_key_strips_trailing_whitespace(tmp_path):
    (tmp_path / "license.key").write_text("v2.payload.sig\n   ", encoding="utf-8")

    result = load_license_key(tmp_path)

    assert result == "v2.payload.sig"


def test_default_license_path(tmp_path):
    assert default_license_path(tmp_path) == tmp_path / "license.key"


def test_resolve_base_dir_frozen():
    # frozen onedir: executable=<root>/bin/fis-monitor -> <root>
    result = resolve_base_dir(
        frozen=True,
        executable=Path("/x/bin/fis-monitor"),
        module_file=Path("/irrelevant/module.py"),
    )
    assert result == Path("/x")


def test_resolve_base_dir_src_layout():
    # src-layout: module_file=<root>/src/fis_monitor/app.py -> <root>
    result = resolve_base_dir(
        frozen=False,
        executable=Path("/irrelevant/python"),
        module_file=Path("/x/src/fis_monitor/app.py"),
    )
    assert result == Path("/x")
