# ADR-021: Manual STARTTLS — обход smtplib server_hostname bug при connect-by-IP (R4-C2)

**Context.** [[decisions/ADR-015-smtp-host-validation|ADR-015]] ext (R3-C4) утверждал что `smtp.starttls(context=ctx)` корректно работает с connect-by-IP: «smtplib передаёт original_host как server_hostname для SNI». Security Engineer (4-й раунд) показал, что это **неверно** — реальный CPython source:

```python
# smtplib.SMTP.starttls(context):
self.sock = context.wrap_socket(self.sock, server_hostname=self._host)
#                                                          ^^^^^^^^^^
```
`self._host` устанавливается в конструкторе `SMTP(host=...)`. Поскольку мы зовём `SMTP(host=endpoint.ip)` (для pin'нутого connect), `server_hostname=ip_literal` — TLS-cert verify валится против IP (cert hostname = `smtp.yandex.ru`, presented host = `87.250.250.X`):
- **Availability-bug**: `ssl.SSLCertVerificationError: Hostname mismatch` → email не отправляется.
- **Если бы мы выключили `check_hostname` для воркэраунда — security-bug**: MITM прозрачен, любой TLS-cert на любой host принимается.

**Decision.** Manual STARTTLS — обходим `smtp.starttls()` и вручную `ssl.wrap_socket` с правильным `server_hostname`:

```python
endpoint = self.host_policy.resolve_and_check(creds.smtp_host, creds.smtp_port)
smtp = smtplib.SMTP(host=endpoint.ip, port=endpoint.port, timeout=connect_timeout)
smtp.ehlo(endpoint.original_host)

if creds.use_starttls:
    code, _ = smtp.docmd("STARTTLS")
    if code != 220:
        raise SmtpStarttlsError(code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    smtp.sock = ctx.wrap_socket(smtp.sock,
                                server_hostname=endpoint.original_host)
    smtp.file = None
    smtp.ehlo(endpoint.original_host)   # повторный EHLO после TLS

smtp.login(creds.smtp_user, creds.smtp_password.get_secret_value())
smtp.sendmail(from_addr, [recipient], msg_bytes)
smtp.quit()
```

Альтернативы рассмотрены и отвергнуты:
- `SMTP(host=endpoint.original_host)` + override `socket.getaddrinfo` через monkeypatch на endpoint.ip — глобальный side-effect.
- `SMTP_SSL` (implicit TLS, 465 port) — Yandex поддерживает, но major bot-аккаунт настроен на 587 STARTTLS. Не меняем UX «port 587 default».
- Дождаться CPython фикса (вероятно `smtplib.SMTP(host_for_sni=...)`) — версии Python 3.12..3.14 баг присутствует.

**Consequences.** TLS-cert verification работает корректно (cert против `smtp.yandex.ru`, connect по pin'нутому IP). MITM/DNS-rebinding закрыт. Цена: ~15 строк boilerplate вместо одного `smtp.starttls()`. Документировать в `SmtpEmailNotifier` docstring почему руками.

См. также: [[decisions-log]], [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[architecture/03-protocols]] §3.3.
