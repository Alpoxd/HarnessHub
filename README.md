# HarnessHub

Терминальный менеджер подписочных AI harness: профили аккаунтов, запуск родных клиентов,
диагностика, квоты и обслуживание Antigravity. TUI и CLI используют общее ядро.

Рабочая версия 0.2. Каталог расширяется пользовательскими адаптерами.

![TUI HarnessHub](docs/preview.svg)

## Запуск

Нужны Python 3.11+ и [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run harnesshub
```

Постоянная команда из любого каталога:

```bash
uv tool install --editable .
harnesshub
```

Без uv:

```bash
python -m pip install .
harnesshub
```

Менеджер обнаруживает клиенты в PATH и в собственном каталоге установок.
У отсутствующего клиента в TUI появляется кнопка «Установить».
Запуск каталога не выполняет загрузку, вход или патчинг.

## Поддержка

| Harness | Обнаружение / родной запуск | Профили | Данные квот |
| --- | --- | --- | --- |
| Claude Code | Да | `CLAUDE_CONFIG_DIR`, документированная изоляция | Локальный трекер кулдаунов + родной клиент |
| OpenAI Codex | Да | `CODEX_HOME`, файловая авторизация | Живой запрос через app-server + кэш слотов |
| Antigravity | Да | Linux/macOS (`HOME`) и Windows (`USERPROFILE`/`APPDATA`) | Тиры `.models` (`g4+5.5`, `g4h+4.6`, `3.8+4.6`) и бакеты квот |
| Qoder CLI | Да | `QODER_CONFIG_DIR`; два аккаунта ещё требуют живой проверки | В родном клиенте |
| Kimi CLI · legacy | Существующий Python-клиент | `KIMI_SHARE_DIR` | В родном клиенте |
| Kimi Code | Управляемая установка | `KIMI_CODE_HOME` | В родном клиенте |
| GitHub Copilot | Да | Родное переключение `/user` | В родном клиенте |
| Cursor Agent | Да | Родная конфигурация | В родном клиенте |
| Gemini CLI | Да | Родная конфигурация | В родном клиенте |
| Kiro CLI | Да | Родная конфигурация | В родном клиенте |
| JetBrains Junie | Да | Родная конфигурация | В родном клиенте |

Для каждого клиента используется его собственный вход по подписке. У управляемых
профилей известных клиентов удаляются унаследованные переменные API-ключей и альтернативных
платформ. Настройки проекта и политики организации остаются под управлением самого harness;
менеджер не подтверждает план подписки по одному наличию каталога.

Перенос конфигурации проверен по документации/исходникам; вход в два реальных аккаунта
на каждой ОС ещё требует отдельной проверки. Локальные тесты выполнены на Linux.

## TUI

Вверху окна расположена общая панель рабочего каталога проекта, переключателя автомода и фонового опроса всех квот. Рабочая область разделена на 4 вкладки:
- **1 · Аккаунты**: слева список харнессов, справа таблица профилей с разбивкой квот по группам (AGY: `Gemini` и `Claude / GPT`; Codex: `Окно 5ч` и `Окно 7д`), тарифом, токенами и статусом, плюс карточка-инспектор со шкалами всех квотных окон и свёрнутым списком моделей аккаунта.
- **2 · По модели**: единый реестр моделей со схлопыванием уровней рассуждений (`low`/`medium`/`high`/`xhigh`) и переключателем уровня, фильтрами по харнессу и семейству (`gpt`, `gemini`, `claude`), ранжированием кандидатов по остатку квоты и пометкой fallback-моделей (`↪`).
- **3 · Диалоги**: полноэкранный поиск и фильтрация по харнессу, аккаунту и проекту, статистика токенов, встроенный просмотр переписки, `Resume`, `Resume как…` (для общих AGY-сессий под другим аккаунтом) и `Handoff`.
- **4 · Настройки**: единая вкладка с карточками сети/прокси и обслуживания AGY (автохил, автомод, общая история, синхронизация) с выбором профиля на месте.

Кнопка «Найти» в панели управления профилями показывает существующие AGY/Codex/Claude-профили и регистрирует выбранный каталог.

- `1`–`4` — быстрое переключение вкладок; `/` — фокус на каталог проекта; `a` — переключить автомод.
- `Tab` / `Shift+Tab` — переход между элементами; стрелки — выбор в списках.
- `n` — новый профиль; `l` — вход; `u` — живой опрос квот и моделей выбранного аккаунта; `r` — обновить.
- `Enter` в таблице профилей — запустить выбранный профиль; `Esc` закрывает диалог; `q` — выход.

Родной CLI получает терминал целиком (через безопасную приостановку TUI без перезапуска процесса). После завершения фокус возвращается в менеджер.
Для терминалов от 80×30 предусмотрена компактная раскладка с прокруткой.

У отсутствующего harness кнопка установки показывает официальный источник и позволяет выбрать общий прокси или прокси конкретного профиля для загрузки.

## Прокси

Приоритет: настройки профиля → общие настройки → окружение терминала.
Для каждого уровня доступны наследование, свой HTTP(S)-прокси и прямое подключение.

```bash
harnesshub proxy global set http://127.0.0.1:10809
harnesshub proxy global show
harnesshub proxy profile agy acc1 set http://127.0.0.1:10810
harnesshub proxy profile codex acc2 direct
harnesshub proxy profile codex acc2 inherit
harnesshub proxy global inherit
```

Исключения задаются через `--no-proxy localhost,127.0.0.1,::1,internal.example`.
У явно заданного прокси по умолчанию исключены loopback-адреса для локального OAuth.
Режим `direct` отключает прокси окружения и общий прокси; режим `inherit` сохраняет
настройки предыдущего уровня. Без настроек поведение окружения остаётся прежним.

Настройки применяются к запуску, входу, квотам Codex и проверке запуска при автохиле.
HTTP_PROXY, HTTPS_PROXY, ALL_PROXY и NO_PROXY передаются в обоих регистрах только
дочерним процессам. Настройки прокси внутри самого harness могут иметь собственный приоритет.
SOCKS-прокси пока не поддерживаются.

URL с авторизацией поддерживается: `http://user:password@host:port`. В TUI поле URL скрыто,
а в JSON и сообщениях менеджера userinfo маскируется. Сам URL хранится в локальном
`state.json` с правами `0600` на POSIX; на Windows действуют права пользовательского каталога.
Вывод родного клиента находится под контролем самого клиента.

## Установка оригинальных клиентов

```bash
harnesshub install qoder --dry-run
harnesshub install qoder
harnesshub install codex -p acc2
harnesshub install agy
harnesshub install kimi-code
```

Без `-p` установщик использует общий прокси. С `-p` — настройки указанного профиля этого
harness. Активный профиль автоматически не выбирается для загрузки.

| Harness | Официальный источник |
| --- | --- |
| Claude | npm `@anthropic-ai/claude-code` |
| Codex | npm `@openai/codex` |
| Qoder | npm `@qoder-ai/qodercli` |
| Kimi Code | npm `@moonshot-ai/kimi-code` |
| Copilot | npm `@github/copilot` |
| Gemini | npm `@google/gemini-cli` |
| AGY | `https://antigravity.google/cli/install.sh` |
| Cursor | `https://cursor.com/install` |
| Kiro | `https://cli.kiro.dev/install` |
| Junie | `https://junie.jetbrains.com/install.sh` |

Для npm используется официальный `registry.npmjs.org`, отдельный prefix и проверка
минимальной версии Node.js. Пользовательские npm-зеркала не применяются. JavaScript
entrypoint запускается через Node; нативный Claude — напрямую, без Windows `.cmd`-обёрток.

На Linux/macOS официальные vendor-установщики выполняются с отдельным HOME внутри
новой установки; реальные настройки аккаунтов при запуске клиента сохраняют свои пути.
На Windows npm-установка подключена полностью (поддерживаются `.exe`, `.js` и shebang-скрипты
`#!/usr/bin/env node`), загрузка скриптов работает через `curl` или встроенный `urllib`,
а для AGY/Cursor/Kiro/Junie скачивается официальный PowerShell-установщик с готовой командой запуска.
Изоляция профилей, блокировки (`LockFileEx`), запуск IDE (`%LOCALAPPDATA%\Programs`), общая история
AGY (NTFS Junction + Hardlink без прав администратора) и UTF-8 вывод консоли работают на Windows нативно;
только встроенный ELF64-патчер автохила AGY остаётся специфичным для Linux x64.

Каждая установка получает собственный каталог. После успеха менеджер активирует новый
клиент; прежняя версия остаётся на диске для уже работающих процессов. При сбое предыдущая
установка сохраняется. Системный PATH не изменяется. Скачанный AGY остаётся оригинальным:
автохил выполняется отдельно по явной команде или при запуске профиля с включённым автохилом.
Команды установки не выполняют авторизацию. Для пользовательских адаптеров источники
автоматической установки не подставляются.

Старый Python `kimi-cli` архивирован и не устанавливается заново. Его существующие профили
сохраняются под ID `kimi`; новый официальный Kimi Code устанавливается под ID `kimi-code`,
имеет отдельную конфигурацию и использует интерактивный `/login`.

## Профили и запуск

```bash
harnesshub list
harnesshub doctor
harnesshub profiles new codex work
harnesshub login codex -p work
harnesshub run codex -p work -C /path/to/project
harnesshub profiles use codex work
harnesshub run codex
harnesshub run claude --native -C /path/to/project
harnesshub run codex -p work -- --model YOUR_MODEL
harnesshub usage codex -p work
```

Вход старого Kimi: `harnesshub login kimi -p work`. Для нового клиента создай профиль
`harnesshub profiles new kimi-code work`, затем выполни `harnesshub login kimi-code -p work`
и введи `/login`. Для Qoder команда входа открывает родной
клиент, где нужно ввести `/login`. Для AGY вход также проходит интерактивно.
Старые Kimi-конфигурации с `storage="keyring"` могут мигрировать общее хранилище
при запуске самого старого Kimi; для нового аккаунта создавай свежий профиль.

```bash
harnesshub profiles discover
harnesshub profiles import agy acc1 /path/to/agy-profiles/acc1
harnesshub profiles import codex main /path/to/.codex
harnesshub profiles list
harnesshub profiles rename codex work office
harnesshub profiles remove codex office
```

Импорт регистрирует путь без копирования токенов. Переименование меняет метку в менеджере,
сохраняя путь конфигурации и привязку credential store. Удаление убирает регистрацию;
файлы, авторизация и диалоги сохраняются. На одном профиле можно одновременно запускать
несколько параллельных диалогов, консультаций и окон IDE (разделяемые блокировки `LOCK_SH`),
а эксклюзивные операции (`login`, удаление профиля) защищены от конфликта с активными сессиями.
Сторонние запуски вне HarnessHub эти блокировки не используют.

## Model-First автовыбор, единая история диалогов, IDE и второе мнение

### 1. Выбор по модели (Model-First) и сортировка по квотам

Вместо ручного перебора профилей можно выбрать целевую модель (`argon`, `hydro`, `orch`,
`opus`, `sonnet`, `sol`, `astra`, `flash`), а менеджер отфильтрует поддерживающие её профили,
проверит блокировки, остаток квоты и кулдауны `429` (сохраняя слоты волны `g4+5.5` свободными
при запуске `flash`), и запустит лучший аккаунт:

```bash
harnesshub accounts
harnesshub accounts codex --live
harnesshub models list -p codex
harnesshub models rank argon
harnesshub models probe --all
harnesshub smart argon -C /path/to/project
harnesshub smart astra --dry-run
```

### 2. Общая история диалогов, Resume и Cross-Harness Handoff

Менеджер индексирует локальные JSONL-транскрипты диалогов из всех зарегистрированных профилей
AGY (`brain/*/transcript.jsonl`), Claude Code (`projects/*/*.jsonl`) и Codex (`sessions/*/*/*/*.jsonl`).
В TUI (вкладка «Диалоги») и CLI доступны просмотр, родное продолжение (`resume`) и перенос
контекста задачи (`handoff`) в другой харнесс, аккаунт или семейство моделей:

```bash
harnesshub sessions list -C /path/to/project
harnesshub sessions list --all
harnesshub sessions show <SESSION_ID>
harnesshub sessions resume <SESSION_ID>
harnesshub sessions handoff <SESSION_ID> -m astra --note "Проверь архитектуру и допиши тесты"
```

### 3. Запуск полноценных IDE в изолированном профиле

Запуск Antigravity IDE (`/usr/bin/antigravity`), VS Code (`/usr/bin/code`) или Cursor
с отдельным `--user-data-dir`, прокси профиля и изолированными переменными окружения
(`HOME`, `DBUS_SESSION_BUS_ADDRESS`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`), чтобы окно Electron
не склеивалось с чужим открытым аккаунтом, но разделяло общий каталог расширений:

```bash
harnesshub ide agy -p acc1 -C /path/to/project
harnesshub ide vscode --agy-profile acc1 --claude-profile main --codex-profile main -C /path/to/project
harnesshub ide agy -p acc1 --dry-run
```

### 4. Второе / третье мнение и встроенный MCP-сервер

Команда `consult` и встроенный stdio MCP-сервер (`harnesshub mcp`) позволяют запрашивать
второе мнение у модели другого семейства или собирать параллельный консилиум (`--council`),
автоматически прикрепляя хвост текущего диалога проекта и `git diff`:

```bash
harnesshub consult -m astra "Оцени текущие изменения на edge-cases"
harnesshub consult --council "Сравни варианты архитектуры"
harnesshub mcp
```

## AGY: патчинг и автохил

Встроенный патчер в этой версии поддерживает Linux ELF64 x64 и механизм двух гейтов
из существующего локального автохила, проверенный на AGY 1.5.16. Ищутся только
исполняемые сегменты; неоднозначная, частичная или неизвестная сигнатура останавливает запись.
Windows, macOS и ARM64 во встроенном автохиле пока не поддерживаются.

```bash
harnesshub agy status
harnesshub agy heal
harnesshub agy autoheal acc1 on
harnesshub agy autoheal acc1 off
harnesshub agy automode on
harnesshub agy share-history on
harnesshub agy sync
harnesshub agy restore
harnesshub agy configure --binary /path/to/agy
```

### Общая история диалогов, автомод и основной профиль AGY

- **Автомод (`--dangerously-skip-permissions` / `--yolo`)**: HarnessHub запускает бинарники напрямую в обход shell-функций `.bashrc`/`.zshrc`. Включить автоматический пропуск подтверждений можно для всех профилей (`harnesshub agy automode on`), точечно для профиля (`-p acc1 on`) или разово при запуске (`harnesshub run agy --yolo` / `--safe`). Read-only `consult` всегда остаётся в `--mode plan`.
- **Единая история чатов AGY (`share-history`)**: По умолчанию каждый изолированный профиль AGY хранит свою отдельную папку `.gemini/antigravity-cli/{brain,conversations,conversation_summaries.db}`. Команда `harnesshub agy share-history on` бережно объединяет диалоги и SQLite-индексы всех незанятых профилей в единый `~/.gemini/antigravity-cli` и связывает их симлинками, сохраняя строгую изоляцию токенов `antigravity-oauth-token`. После этого любой диалог можно продолжить под другим аккаунтом (`harnesshub sessions resume <ID> -p <профиль>`).
- **Синхронизация настроек и импорт `main`**: `harnesshub agy sync` копирует `settings.json` (с автоматической адаптацией модели для тарифов `3.8+4.6`) и привязывает `GEMINI.md`, `.agents` и `.gitconfig` во все профили AGY. Основной каталог пользователя (`~`) обнаруживается через `profiles discover` как `agy/main`.

Автохил выключен у новых и импортированных профилей. После включения выполняется перед
запуском AGY через менеджер. Системный daemon и перехват запусков вне HarnessHub не создаются.

Перед записью сохраняется проверенная SHA-256 копия `<agy>.hh-<hash>.bak`, затем проверяется
временный файл и атомарно заменяется бинарник. Ошибка проверки `--version` вызывает откат.
Манифест `<agy>.harnesshub.json` связывает откат с контрольной суммой установленного патча.
При обновлении бинарника откат старой версии останавливается. Проверка запуска не проверяет
серверную доступность аккаунта. Внешний обновлятор не соблюдает блокировки менеджера.

Можно подключить существующий helper, принимающий путь к бинарнику первым аргументом:

```bash
harnesshub agy configure --helper /path/to/agy-autoheal
```

Внешний helper отвечает за свой бэкап и откат; `restore` обслуживает только бэкапы
встроенного патчера. Проверка результата helper в этой версии также ориентирована на Linux x64.

## Дополнительные harness

```bash
harnesshub harness add mycli --name "My subscription CLI" --command mycli
harnesshub harness add isolated --name "Isolated CLI" --command isolated-cli --config-env CLI_HOME
```

Без `--config-env` адаптер предоставляет родной запуск. С ним доступны отдельные каталоги
профилей; корректность изоляции учётных данных необходимо проверить для конкретного клиента.
Вход можно задать повторяющимся `--login-arg`. Команды запускаются массивом аргументов,
без shell-интерполяции. Установка неизвестных CLI автоматически не выполняется.

## Данные и разработка

Данные по умолчанию хранятся в стандартном пользовательском каталоге платформы
(`~/.local/share/harnesshub` на Linux). Другой каталог: `harnesshub --data-dir PATH …`.
Машиночитаемый вывод: `harnesshub --json list`, `harnesshub --json profiles list`.
Схема реестра версионирована; повреждённый или неизвестный формат не перезаписывается.

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

[Архитектура и границы адаптеров](docs/architecture.md).
