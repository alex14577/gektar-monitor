# ADR-015: SMTP host validation — IP/DNS rules + resolve-recheck

**Context.** Первая версия `SmtpCredentials.host` validator имела дыры: IPv4-mapped IPv6, IPv6 unique-local `fc00::/7`, link-local `fe80::/10`, multicast, cloud-metadata `169.254.169.254`, IPv4-compatible `::a.b.c.d`, `0.0.0.0`/`::`. Также TOCTOU между Pydantic-валидацией и реальным `smtplib.SMTP(host)` — DNS может резолвится в RFC1918 после save.

**Decision.** Разделение domain vs infra:
- **`SmtpCredentials` (domain)** — Pydantic-модель с чистым формат-валидатором (syntactically valid IP/hostname, длина, отсутствие CR/LF).
- **`SmtpHostPolicy` (infra)** — `infra/smtp/host_policy.py`. Универсальное правило через `ipaddress.ip_address(resolved).{is_private|is_loopback|is_link_local|is_multicast|is_reserved|is_unspecified}` + IPv4-mapped IPv6 распаковка + отдельное правило для cloud-metadata + TLD-blocklist (`*.lan`, `*.local`, `*.internal`, `*.corp`, `*.home`, `*.localdomain`, `*.test`, `*.example`, `*.invalid`, `*.localhost`).
- **DNS resolve recheck** через `socket.getaddrinfo(host, port)` — проверка ВСЕХ A/AAAA. Применяется в двух точках: `SettingsService.set_smtp_credentials()` (на save) и `SmtpEmailNotifier.send()` ПЕРЕД connect (на каждый отправку — закрывает TOCTOU).

**Consequences.** Закрывает SSRF-вектор. Цена: каждая отправка email делает дополнительный getaddrinfo (~ms). Domain не знает про infra-policy — корректное разделение.

**Расширение R3-C4 (connect-by-IP + SNI verify).** `SmtpHostPolicy.check()` deprecated в пользу `resolve_and_check(host, port) -> ResolvedSmtpEndpoint`. Без этого оставался TOCTOU: policy делала `getaddrinfo` → проверяла → возвращала None; `smtplib.SMTP(host).connect()` делал **повторный** `getaddrinfo`, и атакующий с DNS-MITM мог вернуть RFC1918 IP между двумя resolve-ами. Теперь `SmtpEmailNotifier.send()` использует `endpoint.ip` для connect (pin'нутый IP), `endpoint.original_host` для `EHLO` и SNI. TLS-cert validation идёт по original hostname через `ssl.create_default_context()` (check_hostname=True) и `starttls(context=ctx)` — smtplib передаёт original_host как `server_hostname` для SNI. Connect-by-IP не ломает TLS — это стандартный паттерн `connect(ip) + verify(hostname)`. `ResolvedSmtpEndpoint` — infra-dataclass, см. [[data-model/notifications]].

См. также: [[decisions-log]], [[decisions/ADR-021-manual-starttls-connect-by-ip|ADR-021]], [[architecture/03-protocols]] §3.3.
