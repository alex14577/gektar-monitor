# ADR-021: Manual STARTTLS + Implicit TLS — обход smtplib server_hostname bug при connect-by-IP (R4-C2, amendment 2026-05-16)

**Context.** [[decisions/ADR-015-smtp-host-validation|ADR-015]] ext (R3-C4) утверждал что `smtp.starttls(context=ctx)` корректно работает с connect-by-IP: «smtplib передаёт original_host как server_hostname для SNI». Security Engineer (4-й раунд) показал, что это **неверно** — реальный CPython source:

```python
# smtplib.SMTP.starttls(context):
self.sock = context.wrap_socket(self.sock, server_hostname=self._host)
#                                                          ^^^^^^^^^^
```
`self._host` устанавливается в конструкторе `SMTP(host=...)`. Поскольку мы зовём `SMTP(host=endpoint.ip)` (для pin'нутого connect), `server_hostname=ip_literal` — TLS-cert verify валится против IP (cert hostname = `smtp.yandex.ru`, presented host = `87.250.250.X`):
- **Availability-bug**: `ssl.SSLCertVerificationError: Hostname mismatch` → email не отправляется.
- **Если бы мы выключили `check_hostname` для воркэраунда — security-bug**: MITM прозрачен, любой TLS-cert на любой host принимается.

## STARTTLS path (port 587)

**Decision.** Manual STARTTLS — обходим `smtp.starttls()` и вручную `ssl.wrap_socket` с правильным `server_hostname`:

```python
smtp = smtplib.SMTP(host=endpoint.ip, port=endpoint.port, timeout=connect_timeout)
smtp.ehlo(endpoint.original_host)
code, _ = smtp.docmd("STARTTLS")
if code != 220:
    raise _StarttlsRefused(code)
ctx = ssl.create_default_context()
ctx.check_hostname = True
smtp.sock = ctx.wrap_socket(smtp.sock, server_hostname=endpoint.original_host)
smtp.file = None   # invalidate cached file-wrapper
smtp.ehlo(endpoint.original_host)   # повторный EHLO после TLS
```

## Implicit TLS path (port 465) — Amendment 2026-05-16

**Bug.** `smtplib.SMTP_SSL(host=endpoint.ip)` имеет ту же проблему: `self._host = endpoint.ip` → `wrap_socket(server_hostname=ip_literal)` → `SSLCertVerificationError`. Пользователи с Yandex/Mail.ru/Yahoo (port 465) получали `SMTPServerDisconnected` — plain TCP в SSL-only порт.

**Decision.** Открыть raw TCP socket на pinned IP, обернуть с правильным SNI через `ctx.wrap_socket`, затем инжектировать в `smtplib.SMTP` экземпляр без auto-connect:

```python
ctx = ssl.create_default_context()
ctx.check_hostname = True
raw_sock = socket.create_connection((endpoint.ip, endpoint.port), timeout=...)
tls_sock = ctx.wrap_socket(raw_sock, server_hostname=endpoint.original_host)
smtp = smtplib.SMTP(timeout=connect_timeout)   # host='' → нет auto-connect
smtp.sock = tls_sock
smtp.file = None
smtp.getreply()   # читает 220-banner
smtp.ehlo(endpoint.original_host)
```

TLS-режим определяется из порта: `port == 465` → implicit TLS; иначе → STARTTLS. Поле `use_starttls` в `SmtpCredentials` не нужно — derive on-the-fly.

Альтернативы рассмотрены и отвергнуты:
- `SMTP(host=endpoint.original_host)` + override `socket.getaddrinfo` — глобальный side-effect.
- `SMTP_SSL` с override `_get_socket` через subclass — более сложно, чем явное socket.create_connection.
- `use_starttls` поле в `SmtpCredentials` + миграция БД — лишняя сложность, derive от порта достаточен.
- Дождаться CPython фикса (`smtplib.SMTP(host_for_sni=...)`) — баг в Python 3.12..3.14.

**Consequences.** TLS-cert verification корректен для обоих path (cert против `smtp.yandex.ru`, connect по pin'нутому IP). MITM/DNS-rebinding закрыт. Оба path в одном модуле (high cohesion), общий error-mapping. Цена: ~30 строк boilerplate.

См. также: [[decisions-log]], [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-038-smtp-provider-catalog|ADR-038]], [[architecture/03-protocols]] §3.3.
