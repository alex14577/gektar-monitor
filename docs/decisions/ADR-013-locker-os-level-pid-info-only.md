# ADR-013: Locker — OS-level lock, PID info-only

**Context.** Single-instance lock через PID-файл уязвим к race condition (PID может быть переиспользован).

**Decision.** Локer ОБЯЗАН использовать OS-level lock: `fcntl.flock(LOCK_EX|LOCK_NB)` на Linux, `msvcrt.locking` на Windows. Файл открывается с `O_NOFOLLOW|O_EXCL`. PID записывается в файл только для info («кто держит лок»).

**Consequences.** Корректная single-instance без race. PID-info полезен для диагностики, не для арбитража.

См. также: [[decisions-log]], [[architecture/03-protocols]] §3.5.
