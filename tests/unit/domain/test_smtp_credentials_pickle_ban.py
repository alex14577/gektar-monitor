"""Tests: SmtpCredentials pickle/deepcopy hard-block — Layer 0 domain invariants.

Security policy: ADR-017, bd issue gektar_monitor-ctz.

Invariants under test:
  1. pickle.dumps() raises TypeError — __reduce__ is blocked.
  2. pickle.loads() with a faked payload raises TypeError — __setstate__ is
     blocked, preventing unpickling of externally-crafted streams.
  3. copy.deepcopy() raises TypeError — __deepcopy__ is blocked.
  4. copy.copy() (shallow copy) is NOT blocked — Pydantic frozen models support
     it via __copy__ without going through __reduce__; no credential bytes cross
     a serialisation boundary.
  5. Non-secret fields are readable after normal construction — the ban does not
     break ordinary usage.

Layer: 0 (domain unit — no I/O, no DB, no mocks needed).
"""

from __future__ import annotations

import copy
import io
import pickle
import struct

import pytest
from pydantic import SecretStr

from fis_monitor.domain.models import SmtpCredentials

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def creds() -> SmtpCredentials:
    return SmtpCredentials(
        smtp_user="alice@example.com",
        smtp_password=SecretStr("hunter2"),
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_default=True,
        from_name="Alice",
    )


# ---------------------------------------------------------------------------
# T1 — pickle.dumps raises TypeError
# ---------------------------------------------------------------------------


class TestPickleDumpsBlocked:
    def test_pickle_dumps_raises_type_error(self, creds: SmtpCredentials) -> None:
        """pickle.dumps() must raise TypeError, not silently serialise the secret."""
        with pytest.raises(TypeError, match="SmtpCredentials cannot be pickled"):
            pickle.dumps(creds)

    def test_pickle_dumps_all_protocols_blocked(self, creds: SmtpCredentials) -> None:
        """Block applies across every pickle protocol (0-5)."""
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with pytest.raises(TypeError):
                pickle.dumps(creds, protocol=protocol)

    def test_error_message_references_adr(self, creds: SmtpCredentials) -> None:
        """Error message must cite ADR-017 so engineers can trace the policy."""
        with pytest.raises(TypeError, match="ADR-017"):
            pickle.dumps(creds)


# ---------------------------------------------------------------------------
# T2 — pickle.loads of externally-crafted stream raises TypeError
# ---------------------------------------------------------------------------


class TestPickleLoadsBlocked:
    def test_setstate_raises_on_unpickle(self) -> None:
        """__setstate__ must raise before any state is applied to a new instance.

        We build a minimal pickle stream (protocol 2) that calls __setstate__
        on a SmtpCredentials instance directly, bypassing __reduce__.

        Stream anatomy (protocol 2):
          PROTO 2
          GLOBAL <module> <class>       -- push the class onto the stack
          EMPTY_TUPLE                   -- ()
          REDUCE                        -- calls class.__new__(class) = bare instance
          EMPTY_DICT                    -- {}
          MARK + SHORT_BINUNICODE*2 + SETITEMS  -- {'smtp_password': 'EVIL'}
          BUILD                         -- calls instance.__setstate__(state_dict)
          STOP
        """
        module = SmtpCredentials.__module__
        qualname = SmtpCredentials.__qualname__

        def _binunicode(s: str) -> bytes:
            """Encode a string as a SHORT_BINUNICODE (opcode 'X', 4-byte LE length)."""
            encoded = s.encode("utf-8")
            return b"X" + struct.pack("<I", len(encoded)) + encoded

        buf = io.BytesIO()
        buf.write(b"\x80\x02")  # PROTO 2
        # GLOBAL: push SmtpCredentials class
        buf.write(b"c" + module.encode() + b"\n" + qualname.encode() + b"\n")
        buf.write(b")")   # EMPTY_TUPLE
        buf.write(b"\x81")  # NEWOBJ → class.__new__(class)  (no __reduce__ call)
        # State dict: {'smtp_password': 'EVIL'}
        buf.write(b"}")   # EMPTY_DICT
        buf.write(b"(")   # MARK
        buf.write(_binunicode("smtp_password"))
        buf.write(_binunicode("EVIL"))
        buf.write(b"u")   # SETITEMS (consumes MARK..top as k/v pairs into dict)
        buf.write(b"b")   # BUILD → instance.__setstate__(state_dict)
        buf.write(b".")   # STOP

        stream = buf.getvalue()
        with pytest.raises(TypeError, match="SmtpCredentials cannot be pickled"):
            pickle.loads(stream)


# ---------------------------------------------------------------------------
# T3 — copy.deepcopy raises TypeError
# ---------------------------------------------------------------------------


class TestDeepcopyBlocked:
    def test_deepcopy_raises_type_error(self, creds: SmtpCredentials) -> None:
        """copy.deepcopy() must raise TypeError — deepcopy → __reduce__ path blocked."""
        with pytest.raises(TypeError, match="SmtpCredentials cannot be pickled"):
            copy.deepcopy(creds)

    def test_deepcopy_with_memo_raises_type_error(self, creds: SmtpCredentials) -> None:
        """Even when a memo dict is supplied, deepcopy must still raise."""
        with pytest.raises(TypeError):
            copy.deepcopy(creds, {})


# ---------------------------------------------------------------------------
# T4 — copy.copy (shallow) is allowed
# ---------------------------------------------------------------------------


class TestShallowCopyAllowed:
    def test_shallow_copy_does_not_raise(self, creds: SmtpCredentials) -> None:
        """Shallow copy is safe — no serialisation boundary, same object references."""
        cloned = copy.copy(creds)
        # The clone is a distinct object but shares the same SecretStr reference.
        assert cloned is not creds
        assert cloned.smtp_user == creds.smtp_user
        assert cloned.smtp_password is creds.smtp_password  # same SecretStr object


# ---------------------------------------------------------------------------
# T5 — ordinary construction and field access still work
# ---------------------------------------------------------------------------


class TestNormalUsageUnaffected:
    def test_field_access_works(self, creds: SmtpCredentials) -> None:
        """The pickle-ban methods must not interfere with normal attribute access."""
        assert creds.smtp_user == "alice@example.com"
        assert creds.smtp_host == "smtp.example.com"
        assert creds.smtp_port == 587
        assert creds.use_default is True
        assert creds.from_name == "Alice"

    def test_secret_value_readable_via_get_secret_value(self, creds: SmtpCredentials) -> None:
        """get_secret_value() — the only legitimate plaintext extraction path — must work."""
        assert creds.smtp_password.get_secret_value() == "hunter2"

    def test_from_name_none_variant(self) -> None:
        """SmtpCredentials with from_name=None constructs without error (ljp compat)."""
        c = SmtpCredentials(
            smtp_user="bob@example.com",
            smtp_password=SecretStr("secret"),
            smtp_host="smtp.example.com",
        )
        assert c.from_name is None
        # Confirm pickle still blocked on this variant too.
        with pytest.raises(TypeError):
            pickle.dumps(c)


# ---------------------------------------------------------------------------
# T6 — Pydantic model_copy paths
# ---------------------------------------------------------------------------


class TestPydanticModelCopy:
    def test_shallow_model_copy_succeeds(self, creds: SmtpCredentials) -> None:
        """model_copy() (shallow) must not raise — it does not cross a serialisation boundary."""
        cloned = creds.model_copy()
        assert cloned is not creds
        assert cloned.smtp_user == creds.smtp_user
        assert cloned.smtp_password is creds.smtp_password  # same SecretStr reference

    def test_deep_model_copy_blocked(self, creds: SmtpCredentials) -> None:
        """model_copy(deep=True) calls __deepcopy__ — must raise TypeError citing ADR-017."""
        with pytest.raises(TypeError, match="ADR-017"):
            creds.model_copy(deep=True)
