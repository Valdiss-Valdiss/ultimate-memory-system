# agents/ — навыки ИИ-агентов для работы с памятью

| Файл | Для кого |
|---|---|
| `skills/memory/SKILL.md` | opencode, Claude Code, Claude (коннектор), Cursor и любые агенты с MCP |
| `skills/hermes/SKILL.md` | агент в Telegram (Hermes и аналоги): подтверждения одним сообщением, голосовой ввод |

Навык кладётся туда, где ваш агент ищет skills (например, `~/.config/opencode/skills/memory/SKILL.md`
для opencode, папка skills Hermes, «Skills» в настройках Claude). Подключение агента к серверу памяти —
`docs/runbooks/runbook-memory-mcp.md` (раздел «Подключение агентов»).

Главное правило навыков: ответ на «найди» собирает **сервер** (`find_notes`), агент пересылает его
дословно — поэтому все агенты отвечают одинаково, а формат меняется в одном месте.
