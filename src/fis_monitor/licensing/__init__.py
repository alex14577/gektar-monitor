"""Public API for the licensing subsystem.

Only re-exports the verification entrypoint and its result types.
Internal modules (_codec, _hmac, _secret, _verify) are not part of the
public namespace and must not be imported by application code; use these
top-level names instead.
"""

from fis_monitor.licensing._verify import LicenseResult as LicenseResult
from fis_monitor.licensing._verify import LicenseStatus as LicenseStatus
from fis_monitor.licensing._verify import verify_license as verify_license

__all__ = ["LicenseResult", "LicenseStatus", "verify_license"]
