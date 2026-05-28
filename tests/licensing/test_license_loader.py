import pytest

from fis_monitor._license_loader import load_license_key


def _make_src_layout(base):
    """Create a fake src-layout: base/src/fis_monitor/app.py."""
    app_py = base / "src" / "fis_monitor" / "app.py"
    app_py.parent.mkdir(parents=True, exist_ok=True)
    app_py.touch()
    return app_py


def test_load_license_key_valid(tmp_path):
    project = tmp_path / "project"
    app_py = _make_src_layout(project)
    (project / "license.key").write_text("v1.payload.sig", encoding="utf-8")

    result = load_license_key(anchor=app_py)

    assert result == "v1.payload.sig"


def test_load_license_key_missing_raises(tmp_path):
    project = tmp_path / "project"
    app_py = _make_src_layout(project)
    # no license.key written intentionally

    with pytest.raises(FileNotFoundError):
        load_license_key(anchor=app_py)


def test_load_license_key_strips_trailing_whitespace(tmp_path):
    project = tmp_path / "project"
    app_py = _make_src_layout(project)
    (project / "license.key").write_text("v1.payload.sig\n   ", encoding="utf-8")

    result = load_license_key(anchor=app_py)

    assert result == "v1.payload.sig"
