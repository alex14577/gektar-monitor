"""Unit tests for DiagnosticsExcludePolicy.

TDD: these tests define the contract before the implementation.
Acceptance criteria:
  1. EXCLUDED_SETTINGS_PATHS removes notifications.email.recipients and
     notifications.email.from_address from the settings dict.
  2. EXCLUDED_DB_FIELDS removes lot_user_state.note and
     notifications.recipient from any row.
  3. REDACTED_DB_FIELDS redacts cycles.error (URL / email / path patterns
     replaced with placeholders); other columns pass through unchanged.
     Non-string error values are returned as-is (no TypeError).
  4. redact_error covers relative traversal paths and Windows paths with
     spaces.
  5. filter_state_keys applies STATE_ALLOWED_KEYS allowlist (fail-closed)
     and drops keys with forbidden substrings as defence-in-depth.
  6. A full pseudo-dump with all sensitive strings produces JSON that
     contains none of the PII strings.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from fis_monitor.services.diagnostics.exclude_policy import DiagnosticsExcludePolicy


@pytest.fixture()
def policy() -> DiagnosticsExcludePolicy:
    return DiagnosticsExcludePolicy()


# ---------------------------------------------------------------------------
# filter_settings — EXCLUDED_SETTINGS_PATHS
# ---------------------------------------------------------------------------


class TestFilterSettings:
    def test_removes_recipients(self, policy: DiagnosticsExcludePolicy) -> None:
        settings = {
            "notifications": {
                "email": {
                    "recipients": ["alice@example.com", "bob@example.com"],
                    "from_address": "noreply@example.com",
                    "host": "smtp.example.com",
                }
            }
        }
        result = policy.filter_settings(settings)
        assert "recipients" not in result["notifications"]["email"]

    def test_removes_from_address(self, policy: DiagnosticsExcludePolicy) -> None:
        settings = {
            "notifications": {
                "email": {
                    "recipients": ["alice@example.com"],
                    "from_address": "noreply@example.com",
                    "host": "smtp.example.com",
                }
            }
        }
        result = policy.filter_settings(settings)
        assert "from_address" not in result["notifications"]["email"]

    def test_preserves_non_pii_email_fields(self, policy: DiagnosticsExcludePolicy) -> None:
        settings = {
            "notifications": {
                "email": {
                    "recipients": ["alice@example.com"],
                    "from_address": "noreply@example.com",
                    "host": "smtp.example.com",
                    "port": 587,
                }
            }
        }
        result = policy.filter_settings(settings)
        assert result["notifications"]["email"]["host"] == "smtp.example.com"
        assert result["notifications"]["email"]["port"] == 587

    def test_does_not_mutate_original(self, policy: DiagnosticsExcludePolicy) -> None:
        settings = {
            "notifications": {
                "email": {
                    "recipients": ["alice@example.com"],
                    "from_address": "noreply@example.com",
                }
            }
        }
        policy.filter_settings(settings)
        # original must be intact
        assert settings["notifications"]["email"]["recipients"] == ["alice@example.com"]

    def test_missing_intermediate_key_is_safe(self, policy: DiagnosticsExcludePolicy) -> None:
        """If notifications.email is absent the call must not raise."""
        settings: dict = {"notifications": {}}
        result = policy.filter_settings(settings)
        assert result == {"notifications": {}}

    def test_completely_empty_settings(self, policy: DiagnosticsExcludePolicy) -> None:
        result = policy.filter_settings({})
        assert result == {}

    def test_top_level_missing_notifications(self, policy: DiagnosticsExcludePolicy) -> None:
        settings = {"interval_minutes": 15}
        result = policy.filter_settings(settings)
        assert result == {"interval_minutes": 15}

    def test_non_dict_intermediate_value_is_safe(self, policy: DiagnosticsExcludePolicy) -> None:
        """Non-dict intermediate (e.g. notifications="disabled") must not raise and
        must be returned unchanged."""
        settings: dict = {"notifications": "disabled", "interval_minutes": 5}
        result = policy.filter_settings(settings)
        assert result["notifications"] == "disabled"
        assert result["interval_minutes"] == 5


# ---------------------------------------------------------------------------
# filter_row — EXCLUDED_DB_FIELDS (lot_user_state.note)
# ---------------------------------------------------------------------------


class TestFilterRowExcluded:
    def test_lot_user_state_note_removed(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 1, "lot_id": 42, "note": "call the agent tomorrow", "tracked": True}
        result = policy.filter_row("lot_user_state", row)
        assert "note" not in result

    def test_lot_user_state_other_fields_preserved(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 1, "lot_id": 42, "note": "secret", "tracked": True}
        result = policy.filter_row("lot_user_state", row)
        assert result["id"] == 1
        assert result["lot_id"] == 42
        assert result["tracked"] is True

    def test_lot_user_state_note_none_still_removed(self, policy: DiagnosticsExcludePolicy) -> None:
        """Even a None note must not appear in the output (field excluded, not just value)."""
        row = {"id": 1, "lot_id": 42, "note": None, "tracked": False}
        result = policy.filter_row("lot_user_state", row)
        assert "note" not in result

    def test_lot_user_state_missing_note_safe(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 1, "lot_id": 42, "tracked": False}
        result = policy.filter_row("lot_user_state", row)
        assert result == {"id": 1, "lot_id": 42, "tracked": False}

    def test_other_table_note_not_removed(self, policy: DiagnosticsExcludePolicy) -> None:
        """Note field in a different table must pass through."""
        row = {"id": 1, "note": "some text"}
        result = policy.filter_row("lots", row)
        assert "note" in result

    def test_does_not_mutate_original_row(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 1, "lot_id": 42, "note": "private", "tracked": True}
        policy.filter_row("lot_user_state", row)
        assert "note" in row

    # --- C1: notifications.recipient ---

    def test_notifications_recipient_removed(self, policy: DiagnosticsExcludePolicy) -> None:
        """notifications.recipient is PII and must be excluded entirely."""
        row = {"id": 5, "recipient": "leaker@example.com", "status": "pending"}
        result = policy.filter_row("notifications", row)
        assert "recipient" not in result

    def test_notifications_recipient_value_absent_from_output(
        self, policy: DiagnosticsExcludePolicy
    ) -> None:
        row = {"id": 6, "recipient": "leaker@example.com", "status": "sent"}
        result = policy.filter_row("notifications", row)
        assert "leaker@example.com" not in str(result)

    def test_notifications_other_fields_preserved(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 7, "recipient": "x@y.com", "status": "sent", "attempt_no": 1}
        result = policy.filter_row("notifications", row)
        assert result["id"] == 7
        assert result["status"] == "sent"
        assert result["attempt_no"] == 1

    def test_notifications_recipient_none_still_removed(
        self, policy: DiagnosticsExcludePolicy
    ) -> None:
        row = {"id": 8, "recipient": None, "status": "pending"}
        result = policy.filter_row("notifications", row)
        assert "recipient" not in result


# ---------------------------------------------------------------------------
# filter_row — REDACTED_DB_FIELDS (cycles.error)
# ---------------------------------------------------------------------------


class TestFilterRowRedacted:
    def test_cycles_error_is_present_but_redacted(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 1, "error": "failed at https://gosauctions.ru/lot/123 with token=abc"}
        result = policy.filter_row("cycles", row)
        assert "error" in result  # field kept, not removed

    def test_cycles_error_url_redacted(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 7, "error": "failed at https://gosauctions.ru/lot/123 with token=abc"}
        result = policy.filter_row("cycles", row)
        assert "gosauctions.ru" not in result["error"]

    def test_cycles_error_email_redacted(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 8, "error": "recipient bob@example.com failed"}
        result = policy.filter_row("cycles", row)
        assert "bob@example.com" not in result["error"]

    def test_cycles_error_none_returns_none(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 9, "error": None}
        result = policy.filter_row("cycles", row)
        assert result["error"] is None

    def test_cycles_non_error_fields_preserved(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"id": 3, "status": "error", "error": "https://gosauctions.ru/lot/1"}
        result = policy.filter_row("cycles", row)
        assert result["id"] == 3
        assert result["status"] == "error"

    def test_cycles_error_without_pii_unchanged(self, policy: DiagnosticsExcludePolicy) -> None:
        """A plain error message with no PII tokens should survive as-is (or redacted form
        should at minimum not contain any PII — here there is none so it passes either way)."""
        row = {"id": 10, "error": "timeout after 30s"}
        result = policy.filter_row("cycles", row)
        assert "timeout" in result["error"]  # benign content preserved

    def test_other_table_error_not_redacted(self, policy: DiagnosticsExcludePolicy) -> None:
        """error field in a different table must not be redacted by this policy."""
        row = {"id": 1, "error": "https://gosauctions.ru/lot/456"}
        result = policy.filter_row("lots", row)
        # lots.error is not in REDACTED_DB_FIELDS — passes through
        assert result["error"] == "https://gosauctions.ru/lot/456"

    def test_cycles_error_non_string_int_passthrough(
        self, policy: DiagnosticsExcludePolicy
    ) -> None:
        """Non-string error values (legacy migration artifacts) must not raise TypeError."""
        row = {"id": 11, "error": 42, "status": "error"}
        result = policy.filter_row("cycles", row)
        assert result["error"] == 42

    def test_cycles_error_non_string_zero_passthrough(
        self, policy: DiagnosticsExcludePolicy
    ) -> None:
        row = {"id": 12, "error": 0}
        result = policy.filter_row("cycles", row)
        assert result["error"] == 0


# ---------------------------------------------------------------------------
# redact_error — static method
# ---------------------------------------------------------------------------


class TestRedactError:
    def test_none_returns_none(self) -> None:
        assert DiagnosticsExcludePolicy.redact_error(None) is None

    def test_url_https_redacted(self) -> None:
        result = DiagnosticsExcludePolicy.redact_error(
            "failed at https://gosauctions.ru/lot/123 with token=abc"
        )
        assert result is not None
        assert "gosauctions.ru" not in result
        assert "https://gosauctions.ru" not in result

    def test_url_http_redacted(self) -> None:
        result = DiagnosticsExcludePolicy.redact_error("see http://example.com/path?q=1")
        assert result is not None
        assert "example.com" not in result

    def test_email_redacted(self) -> None:
        result = DiagnosticsExcludePolicy.redact_error("recipient bob@example.com failed")
        assert result is not None
        assert "bob@example.com" not in result

    def test_file_path_unix_redacted(self) -> None:
        result = DiagnosticsExcludePolicy.redact_error("error in /home/user/secrets/key.pem")
        assert result is not None
        assert "/home/user/secrets/key.pem" not in result

    def test_file_path_windows_redacted(self) -> None:
        result = DiagnosticsExcludePolicy.redact_error(r"error in C:\Users\alex\AppData\key.pem")
        assert result is not None
        assert "alex" not in result

    def test_empty_string_returns_empty(self) -> None:
        result = DiagnosticsExcludePolicy.redact_error("")
        assert result == ""

    def test_plain_text_no_pii_preserved(self) -> None:
        text = "timeout after 30 seconds"
        result = DiagnosticsExcludePolicy.redact_error(text)
        assert result == text

    # --- M1: relative traversal ---

    def test_redact_error_relative_path(self) -> None:
        text = "open failed at ../../../etc/passwd: no such file"
        result = DiagnosticsExcludePolicy.redact_error(text)
        assert result is not None
        assert "passwd" not in result

    def test_redact_error_relative_path_single_step(self) -> None:
        text = "error reading ../config/secrets.env"
        result = DiagnosticsExcludePolicy.redact_error(text)
        assert result is not None
        assert "secrets.env" not in result

    # --- M2: Windows paths with spaces ---

    def test_redact_error_windows_path_with_spaces(self) -> None:
        text = r"permission denied at C:\Program Files\sensitive\key.pem"
        result = DiagnosticsExcludePolicy.redact_error(text)
        assert result is not None
        assert "sensitive" not in result
        assert "key.pem" not in result

    def test_redact_error_windows_path_program_files(self) -> None:
        text = r"loading C:\Program Files\MyApp\config.dat failed"
        result = DiagnosticsExcludePolicy.redact_error(text)
        assert result is not None
        assert "MyApp" not in result
        assert "config.dat" not in result

    # --- M3: IPv6 host with path ---

    def test_redact_error_ipv6_with_path(self) -> None:
        """[::1]/api/secret must be fully redacted — host must not remain visible."""
        result = DiagnosticsExcludePolicy.redact_error("connect error at [::1]/api/secret")
        assert result is not None
        assert "[::1]" not in result
        assert "api/secret" not in result
        assert "[REDACTED]" in result

    def test_redact_error_ipv6_without_path(self) -> None:
        """Bare bracketed IPv6 address with no path must also be redacted."""
        result = DiagnosticsExcludePolicy.redact_error("timeout connecting to [2001:db8::1]")
        assert result is not None
        assert "2001:db8::1" not in result

    def test_redact_error_ipv6_loopback_only(self) -> None:
        """[::1] with no trailing path is caught by the IPv6 pattern."""
        result = DiagnosticsExcludePolicy.redact_error("refused by [::1]")
        assert result is not None
        assert "[::1]" not in result


# ---------------------------------------------------------------------------
# filter_state_keys
# ---------------------------------------------------------------------------


class TestFilterStateKeys:
    def test_allowed_key_passes_through(self, policy: DiagnosticsExcludePolicy) -> None:
        rows = [{"key": "monitor_paused", "value": "false"}]
        result = policy.filter_state_keys(rows)
        assert len(result) == 1
        assert result[0]["key"] == "monitor_paused"

    def test_all_allowed_keys_pass(self, policy: DiagnosticsExcludePolicy) -> None:
        allowed = ["monitor_paused", "last_full_scan_at", "onboarded", "onboarding_step"]
        rows = [{"key": k, "value": "v"} for k in allowed]
        result = policy.filter_state_keys(rows)
        assert len(result) == 4

    def test_unknown_key_excluded(self, policy: DiagnosticsExcludePolicy) -> None:
        """last_critical_event:smtp is not in the allowlist — must be dropped."""
        rows = [
            {"key": "last_critical_event:smtp", "value": "2024-01-01"},
            {"key": "monitor_paused", "value": "true"},
        ]
        result = policy.filter_state_keys(rows)
        assert len(result) == 1
        assert result[0]["key"] == "monitor_paused"

    def test_forbidden_substring_excluded_even_if_in_allowlist(
        self, policy: DiagnosticsExcludePolicy
    ) -> None:
        """Defence-in-depth: api_token must be dropped by the substring rule
        even if it were somehow added to STATE_ALLOWED_KEYS."""
        # Temporarily patch to simulate a mistake
        original = DiagnosticsExcludePolicy.STATE_ALLOWED_KEYS
        patched = original | frozenset({"api_token"})
        DiagnosticsExcludePolicy.STATE_ALLOWED_KEYS = patched  # type: ignore[assignment]
        try:
            rows = [{"key": "api_token", "value": "supersecret"}]
            result = policy.filter_state_keys(rows)
            assert result == []
        finally:
            DiagnosticsExcludePolicy.STATE_ALLOWED_KEYS = original  # type: ignore[assignment]

    def test_empty_list_returns_empty(self, policy: DiagnosticsExcludePolicy) -> None:
        assert policy.filter_state_keys([]) == []

    def test_row_without_key_field_excluded(self, policy: DiagnosticsExcludePolicy) -> None:
        """Rows missing the 'key' field are dropped (fail-closed)."""
        rows = [{"value": "orphan_value"}]
        result = policy.filter_state_keys(rows)
        assert result == []

    def test_original_rows_not_mutated(self, policy: DiagnosticsExcludePolicy) -> None:
        row = {"key": "monitor_paused", "value": "true"}
        policy.filter_state_keys([row])
        assert row == {"key": "monitor_paused", "value": "true"}

    def test_password_substring_excluded(self, policy: DiagnosticsExcludePolicy) -> None:
        rows = [{"key": "smtp_password_hash", "value": "hashed"}]
        result = policy.filter_state_keys(rows)
        assert result == []

    def test_secret_substring_excluded(self, policy: DiagnosticsExcludePolicy) -> None:
        rows = [{"key": "oauth_secret", "value": "abc123"}]
        result = policy.filter_state_keys(rows)
        assert result == []


# ---------------------------------------------------------------------------
# End-to-end: pseudo-dump scenario
# ---------------------------------------------------------------------------


class TestPseudoDump:
    """Apply policy to a realistic dump dict and assert no PII leaks through
    JSON serialisation."""

    PII_STRINGS: ClassVar[list[str]] = [
        "alice@example.com",
        "bob@example.com",
        "noreply@sender.com",
        "call the agent tomorrow",
        "https://gosauctions.ru/lot/secret-99",
        "recipient admin@corp.com failed",
        "leaker@example.com",  # C1: notifications.recipient
    ]

    def _build_dump(self) -> dict:
        return {
            "settings": {
                "notifications": {
                    "email": {
                        "recipients": ["alice@example.com", "bob@example.com"],
                        "from_address": "noreply@sender.com",
                        "host": "smtp.corp.com",
                        "port": 465,
                    }
                },
                "interval_minutes": 15,
            },
            "tables": {
                "lot_user_state": [
                    {"id": 1, "lot_id": 10, "note": "call the agent tomorrow", "tracked": True},
                    {"id": 2, "lot_id": 11, "note": None, "tracked": False},
                ],
                "cycles": [
                    {
                        "id": 100,
                        "status": "error",
                        "error": "failed at https://gosauctions.ru/lot/secret-99",
                    },
                    {
                        "id": 101,
                        "status": "error",
                        "error": "recipient admin@corp.com failed",
                    },
                    {"id": 102, "status": "ok", "error": None},
                ],
                "notifications": [
                    {"id": 10, "recipient": "leaker@example.com", "status": "pending"},
                    {"id": 11, "recipient": "another@example.com", "status": "sent"},
                ],
                "lots": [
                    {"id": 10, "cadastral_no": "77:01:0001001:1", "status": "active"},
                ],
            },
        }

    def test_no_pii_in_json_output(self, policy: DiagnosticsExcludePolicy) -> None:
        dump = self._build_dump()

        filtered_settings = policy.filter_settings(dump["settings"])
        filtered_tables: dict[str, list[dict]] = {}
        for table, rows in dump["tables"].items():
            filtered_tables[table] = [policy.filter_row(table, row) for row in rows]

        output = json.dumps({"settings": filtered_settings, "tables": filtered_tables})

        for pii in self.PII_STRINGS:
            assert pii not in output, f"PII leaked: {pii!r}"
