"""Public API for the licensing subsystem.

Re-exports the verification entrypoint and its result types.

Internal modules (_codec, _hmac, _verify) are implementation details
and should not be imported by application code.

Exception — composition root: `_secret._assemble_secret` is allowed to
be imported by `fis_monitor.app:main` only (see spec §10). This is the
single materialization point for the HMAC secret in production code.
"""

from fis_monitor.licensing._verify import LicenseResult as LicenseResult
from fis_monitor.licensing._verify import LicenseStatus as LicenseStatus
from fis_monitor.licensing._verify import verify_license as verify_license

__all__ = ["LicenseResult", "LicenseStatus", "verify_license"]
