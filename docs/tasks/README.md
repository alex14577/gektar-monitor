# Task notes

По одной странице на bd-таску: `<bd-id>.md` (например, `gektar_monitor-531.3.md`).

## Шаблон

```markdown
---
bd-id: gektar_monitor-XXX
title: <короткое название>
status: closed
closed: YYYY-MM-DD
files:
  - path/to/file.py
  - path/to/test.py
---

# <title>

## Что сделано
Кратко по сути.

## Почему так
Ключевые решения и trade-offs. Ссылки на [[architecture]] §X.Y, [[decisions-log]] ADR-NN.

## Связи
- Закрывает: `bd #XXX`
- Связано: [[other-task-bd-id]], [[architecture]]
- Новые термины: [[glossary#TermName]]

## Follow-up
Что осталось / какие задачи разблокированы.
```
