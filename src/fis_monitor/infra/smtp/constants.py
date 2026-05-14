"""SMTP infra-level constants. Per ADR-020 SSOT for SMTP credentials is state.db,
NOT the domain Settings model — these defaults are fallbacks when state.db
credentials don't specify host/port (operator skipped onboarding SMTP step).
"""

DEFAULT_SMTP_HOST = "smtp.yandex.ru"
DEFAULT_SMTP_PORT = 587
