# ADR-013: Locker — OS-level lock, PID info-only

**Context.** Single-instance lock через PID-файл уязвим к race condition (PID может быть переиспользован).

**Decision.** Локer ОБЯЗАН использовать OS-level lock: `fcntl.flock(LOCK_EX|LOCK_NB)` на Linux, `msvcrt.locking(LK_NBLCK)` на Windows. Файл открывается с `O_NOFOLLOW` (Unix; защита от symlink-атак). `O_EXCL` намеренно **НЕ** используется — комбинация с `O_CREAT` блокировала бы re-acquire stale-lock файла после краша процесса (acceptance criterion: stale-lock recovery). PID записывается в файл только для info («кто держит лок»).

**Consequences.** Корректная single-instance без race. PID-info полезен для диагностики, не для арбитража. Stale-lock после краша автоматически восстанавливается: OS-lock не удержан → новый `acquire()` успешен → PID перезаписывается.

См. также: [[decisions-log]], [[architecture/03-protocols]] §3.5.
