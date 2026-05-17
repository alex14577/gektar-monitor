"""Smoke tests for web templates integration (oxy.7).

Verifies:
1. All expected template files were copied from claude-design/.
2. Static assets (app.css, app.js) exist.
3. build_templates() returns a properly configured Jinja2Templates instance
   whose environment can load and compile the key templates without error.
4. Each call to build_templates() returns a distinct object (no hidden singleton).
5. Extracted JS static files exist (bd gektar_monitor-dus6).
6. Templates contain no executable inline <script> blocks — only src= or
   type="application/json" data islands are permitted (CSP script-src 'self').
"""
from __future__ import annotations

import re

from fastapi.templating import Jinja2Templates

from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR, build_templates

# ---------------------------------------------------------------------------
# Required template paths relative to TEMPLATES_DIR
# ---------------------------------------------------------------------------
_REQUIRED_TEMPLATES = [
    "base.html.jinja",
    "feed.html.jinja",
    "partials/_header_status.html.jinja",
    "partials/_lot_list.html.jinja",
    "partials/_lot_poster.html.jinja",
    "partials/_sidebar_filters.html.jinja",
    "onboarding/wizard.html.jinja",
    "onboarding/_step1.html.jinja",
    "onboarding/_step2.html.jinja",
    "onboarding/_step3.html.jinja",
    "onboarding/_step4.html.jinja",
]

# Templates for which we run a full parse/compile smoke check.
_COMPILE_SMOKE_TEMPLATES = [
    "base.html.jinja",
    "feed.html.jinja",
    "onboarding/_step1.html.jinja",
    "onboarding/_step2.html.jinja",
    "onboarding/_step3.html.jinja",
    "onboarding/_step4.html.jinja",
]


def test_templates_dir_exists_and_contains_required_files() -> None:
    """All expected template files exist on disk after the oxy.7 copy step."""
    assert TEMPLATES_DIR.is_dir(), f"Templates dir not found: {TEMPLATES_DIR}"
    for rel in _REQUIRED_TEMPLATES:
        path = TEMPLATES_DIR / rel
        assert path.is_file(), f"Missing template: {path}"


def test_static_dir_contains_app_css_and_app_js() -> None:
    """app.css and app.js exist in the static directory."""
    assert STATIC_DIR.is_dir(), f"Static dir not found: {STATIC_DIR}"
    assert (STATIC_DIR / "app.css").is_file(), "Missing static/app.css"
    assert (STATIC_DIR / "app.js").is_file(), "Missing static/app.js"
    # base.html.jinja references favicon.svg via url_for('static', ...) — without
    # the file present, the StaticFiles mount (Wave 9 / 8ov.3) would 500 on the
    # first browser render.
    assert (STATIC_DIR / "favicon.svg").is_file(), "Missing static/favicon.svg"


def test_build_templates_returns_configured_jinja2templates() -> None:
    """build_templates() returns a Jinja2Templates with a working environment.

    Smoke-compiles key templates to catch syntax errors early.
    Filter-resolution happens at render time (not compile time), so templates
    that reference custom filters (e.g. ``to_ago``) will still parse without
    raising TemplateSyntaxError here.
    """
    tpl = build_templates()
    assert isinstance(tpl, Jinja2Templates), "Expected Jinja2Templates instance"
    assert hasattr(tpl, "env"), "Jinja2Templates must expose .env"

    for name in _COMPILE_SMOKE_TEMPLATES:
        # get_template() compiles the template AST — catches syntax errors.
        tpl.env.get_template(name)  # raises TemplateSyntaxError on bad syntax


def test_build_templates_returns_new_instance_each_call() -> None:
    """Each call to build_templates() returns a distinct object.

    Pins the SRP/DI invariant: no hidden module-level singleton; the caller
    (composition root) owns the lifecycle of the Jinja2Templates instance.
    """
    a = build_templates()
    b = build_templates()
    assert a is not b, "build_templates() must not return a cached singleton"


# ---------------------------------------------------------------------------
# Extracted JS static files (bd gektar_monitor-dus6)
# ---------------------------------------------------------------------------

_EXTRACTED_JS_FILES = [
    "feed.js",
    "scope_subjects.js",
    "onboarding_step2.js",
    "settings.js",
]


def test_extracted_js_files_exist() -> None:
    """All four extracted JS static files must be present and non-empty."""
    for name in _EXTRACTED_JS_FILES:
        path = STATIC_DIR / name
        assert path.is_file(), f"Missing extracted JS file: {path}"
        assert path.stat().st_size > 0, f"Extracted JS file is empty: {path}"


# ---------------------------------------------------------------------------
# No executable inline scripts in templates (CSP script-src 'self')
# ---------------------------------------------------------------------------

# Regex: matches a <script ...> opening tag that does NOT have type="application/json"
# and does NOT have a src= attribute — i.e. an executable inline script block.
_INLINE_EXEC_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)(?![^>]*type=['\"]application/json['\"])[^>]*>",
    re.IGNORECASE,
)

_TEMPLATES_TO_CHECK = [
    "feed.html.jinja",
    "partials/_scope_and_subjects.html.jinja",
    "onboarding/_step2.html.jinja",
    "settings.html.jinja",
]


def test_no_executable_inline_scripts_in_templates() -> None:
    """All four modified templates must have zero executable inline <script> blocks.

    CSP invariant: script-src 'self' allows only external scripts.
    Data islands (<script type="application/json">) are permitted — they are
    not executable and CSP does not restrict them.
    """
    for rel in _TEMPLATES_TO_CHECK:
        content = (TEMPLATES_DIR / rel).read_text(encoding="utf-8")
        matches = _INLINE_EXEC_SCRIPT_RE.findall(content)
        assert not matches, (
            f"Template {rel} still contains executable inline <script> tag(s): "
            f"{matches!r} — extract to /static/*.js"
        )
