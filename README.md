# owa-mail-mcp

MCP-сервер для почты Microsoft Exchange / OWA. Даёт AI-ассистенту (Claude Code, Claude Desktop, Codex, Cursor и любому stdio MCP-клиенту) доступ к почтовому ящику:

- **проверять почту:** новые и непрочитанные письма;
- **искать сообщения:** по тексту, отправителю, получателю, теме, датам, вложениям;
- **читать и суммаризировать треды:** вся переписка из «Входящих», «Отправленных» и других папок, по порядку, без процитированной истории;
- **находить отправителей:** кто пишет и сколько, кто ждёт ответа, поиск людей в адресной книге организации;
- **отправлять письма**, но только после того, как пользователь нажмёт «Отправить» в отдельном окне на своём компьютере.

<p align="center"><img src="docs/confirm-light.png" alt="Окно подтверждения отправки" width="620"></p>

Идея взята из [mac-owa-widget](https://github.com/ilyabazhenov/mac-owa-widget): у него MCP для календаря Exchange, где всё только читается, а единственное изменяющее действие (`create_meeting`) подтверждается в окне самого приложения. Здесь та же модель применена к почте.

## Быстрый старт

Нужен Python 3.10+ и доступ к Exchange (если почта открывается только из корпоративной сети, включите VPN).

```bash
pipx install "owa-mail-mcp[keyring] @ git+https://github.com/Khomiakov7706/owa-mail-mcp"
# или: uv tool install "owa-mail-mcp[keyring] @ git+https://github.com/Khomiakov7706/owa-mail-mcp"
```

Сначала можно попробовать без Exchange, на встроенном демо-ящике:

```bash
claude mcp add owa-mail -e OWA_MAIL_BACKEND=demo -e OWA_MAIL_ALLOW_SEND=1 -- owa-mail-mcp
```

### Подключение к Exchange

```bash
export OWA_MAIL_SERVER=mail.company.com          # тот же адрес, что у веб-почты OWA
export OWA_MAIL_EMAIL=ivan.petrov@company.com
export OWA_MAIL_USERNAME='COMPANY\ipetrov'        # или ivan.petrov@company.com
owa-mail-mcp set-password                         # пароль сохраняется в Keychain / Credential Manager / Secret Service
owa-mail-mcp check                                # проверка: имя, число непрочитанных, три последних письма
```

Затем подключите сервер к клиенту:

```bash
claude mcp add owa-mail \
  -e OWA_MAIL_SERVER=mail.company.com \
  -e OWA_MAIL_EMAIL=ivan.petrov@company.com \
  -e 'OWA_MAIL_USERNAME=COMPANY\ipetrov' \
  -e OWA_MAIL_ALLOW_SEND=1 \
  -- owa-mail-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "owa-mail": {
      "command": "owa-mail-mcp",
      "env": {
        "OWA_MAIL_SERVER": "mail.company.com",
        "OWA_MAIL_EMAIL": "ivan.petrov@company.com",
        "OWA_MAIL_USERNAME": "COMPANY\\ipetrov",
        "OWA_MAIL_ALLOW_SEND": "1"
      }
    }
  }
}
```

Codex: `codex mcp add owa-mail --env OWA_MAIL_SERVER=... -- owa-mail-mcp`.

### Настройки

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `OWA_MAIL_SERVER` | — | Хост (`mail.company.com`), адрес OWA (`https://mail.company.com/owa`) или полный URL EWS |
| `OWA_MAIL_EMAIL` | — | Ваш адрес |
| `OWA_MAIL_USERNAME` | `OWA_MAIL_EMAIL` | `DOMAIN\login` или UPN |
| `OWA_MAIL_PASSWORD` | из keyring | Пароль; лучше хранить в keyring (`owa-mail-mcp set-password`) |
| `OWA_MAIL_AUTH` | `ntlm` | `ntlm`, `basic`, `kerberos`/`gssapi`, `sspi`, `digest` |
| `OWA_MAIL_ALLOW_SEND` | `0` | Без этого флага `send_email` даже не показывается ассистенту |
| `OWA_MAIL_CONFIRM_TIMEOUT` | `50` | Сколько секунд окно ждёт решения. Большинство клиентов прерывают вызов через 60 с |
| `OWA_MAIL_INTERNAL_DOMAINS` | — | Дополнительные «свои» домены через запятую (не помечаются как внешние) |
| `OWA_MAIL_OPEN_BROWSER` | `1` | `0`: адрес окна выводится в stderr, браузер не открывается |
| `OWA_MAIL_UI_PORT` | случайный | Порт окна подтверждения на `127.0.0.1` |
| `OWA_MAIL_BACKEND` | `ews` | `demo`: встроенный демо-ящик без сети |

Если сертификат Exchange выпущен корпоративным CA, укажите его: `REQUESTS_CA_BUNDLE=/path/to/corp-ca.pem`.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `get_status` | Кто пользователь (адрес, имя), текущее время, число непрочитанных, включена ли отправка |
| `check_mail` | Новые письма во «Входящих» (по умолчанию непрочитанные) с превью |
| `search_messages` | Поиск по тексту, отправителю, получателю, теме, датам (`7d`, `yesterday`, `2026-10-01`), вложениям, папкам |
| `get_message` | Полный текст письма (HTML переводится в текст), список вложений |
| `get_thread` | Вся переписка по письму, по порядку, без процитированной истории, со списком участников |
| `find_people` | Поиск в адресной книге организации (GAL) и контактах: имя, адрес, должность, отдел |
| `list_senders` | Кто писал за период: число писем, непрочитанные, последняя тема; для `sent` — кому писали вы |
| `send_email` | Новое письмо или ответ (`reply_to_message_id`, `reply_all`) **после подтверждения в окне** |

Промпты: `summarize_thread` (сводка переписки) и `triage_inbox` (разбор входящих: на что ответить, что для сведения).

Пример запроса к ассистенту: *«Что нового в почте со вчерашнего дня? Сделай сводку по треду про бюджет Q4 и подготовь ответ Анне, что оценку пришлю завтра»*. Ассистент вызовет `check_mail`, `get_thread`, затем `send_email`; в браузере откроется окно, где можно отправить письмо, отказаться или поправить текст и получателей.

## Как устроено подтверждение отправки

```
MCP-клиент ──stdio──► owa-mail-mcp ──EWS (HTTPS, NTLM)──► Exchange
                           │
                           └── http://127.0.0.1:<порт>/t/<секрет>/  ◄── браузер пользователя
```

1. `send_email` проверяет письмо: адреса только ASCII (кириллическая «е» в домене не пройдёт), запрещены управляющие символы направления текста (с ними `moc.live@ivan` выглядит как `ivan@evil.com`), лимит получателей, непустые тема и текст. Невидимые символы и скрытые копии выделяются предупреждением, внешние получатели — красным.
2. Письмо попадает в очередь (не больше трёх ожидающих), и сервер открывает в браузере локальную страницу. Её адрес содержит случайный секрет, который ассистент не видит.
3. Пока пользователь думает, вызов ждёт; клиентам, которые это поддерживают, раз в секунду отправляется progress.
4. **Отправить** — письмо уходит, ассистент получает `sent: true` и то, что реально отправлено. **Изменить** — пользователь правит получателей, тему и текст; уходит исправленная версия, ассистент получает `edited_by_user: true`. **Не отправлять** или истёкшее время — ассистент получает ошибку «ничего не отправлено, не повторяй без просьбы».
5. Повтор того же вызова в течение 10 минут возвращает первый результат (`duplicate: true`), а не второе письмо.

Ответ на кнопку приходит только из окна, никогда из MCP-клиента: модель можно уговорить чем угодно через текст письма, которое она только что прочитала. Окно слушает только loopback, не открывается во фрейме (`X-Frame-Options`, CSP `frame-ancestors 'none'`), принимает только JSON и проверяет `Host`/`Origin`, так что чужая веб-страница или DNS rebinding не нажмут кнопку за вас.

**Граница защиты:** если у агента есть неограниченный shell от вашего пользователя, он может прочитать и пароль из keyring, и адрес окна. Подтверждение защищает от агента, которым манипулируют через содержимое писем, но не от агента, у которого уже есть полный доступ к машине.

## Другие меры защиты

- **Только чтение по умолчанию.** Отправка включается отдельной переменной.
- **Защита от блокировки учётной записи.** После первого отказа Exchange в пароле сервер перестаёт ходить в Exchange до перезапуска, поэтому зациклившийся агент не заблокирует доменную учётку в AD. Есть и ограничение частоты: 120 запросов в минуту, не больше двух одновременно.
- **Содержимое писем — данные.** Инструкции сервера и описания инструментов прямо говорят модели не выполнять указания из писем.
- **stdout только для протокола.** Логи идут в stderr, в них нет тем, адресов и текстов писем.
- **Только HTTPS.** На `http://` сервер не отправит пароль.

## Почему EWS, а не OWA `service.svc`

OWA и EWS живут на одном сервере (`/owa` и `/EWS/Exchange.asmx`) и принимают одни и те же доменные учётные данные. mac-owa-widget читает календарь через внутренние JSON-вызовы веб-клиента OWA (`/owa/service.svc?action=GetCalendarView` с токеном `X-OWA-CANARY` и заголовками, снятыми из HAR), а для записи (создание встречи, ответ на приглашение) всё равно использует EWS SOAP. Для почты EWS удобнее: это документированный стабильный API, есть зрелая библиотека [exchangelib](https://github.com/ecederstrand/exchangelib), а у `service.svc` формат запросов меняется от версии к версии.

Если в вашей организации EWS закрыт, а OWA работает, бэкенд можно заменить: инструменты работают через интерфейс `MailBackend` (`src/owa_mail_mcp/backend.py`), и бэкенд на `service.svc` пишется отдельно, без изменений в инструментах и окне подтверждения.

Для Exchange Online (Microsoft 365) Microsoft отключает Basic auth и выводит EWS из эксплуатации; для облачной почты лучше взять сервер на Microsoft Graph (см. ниже).

## Готовые решения, которые стоит знать

Перед созданием проекта я посмотрел, что уже есть. Звёзды и активность проверяйте сами: доступ к поиску GitHub из этой среды был ограничен, часть данных взята из каталогов MCP-серверов.

| Проект | Протокол | Подходит для | Как с отправкой |
|---|---|---|---|
| [a-lagutov/outlook-ews-mcp](https://github.com/a-lagutov/outlook-ews-mcp), [viartemev/outlook-ews-mcp](https://github.com/viartemev/outlook-ews-mcp) | EWS (exchangelib), NTLM/Basic | On-prem Exchange: почта, календарь, контакты, вложения | Черновики + `send_draft`; подтверждение на стороне клиента |
| [ShermanGu/exchange-ews-mcp](https://glama.ai/mcp/servers/ShermanGu/exchange-ews-mcp) | EWS, NTLM, Windows | On-prem Exchange | Draft-first: письма создаются только как черновики |
| [askarzh/exchange-mcp](https://github.com/askarzh/exchange-mcp) | EWS + демон на Postgres | Архив и поиск по большому ящику | `SEND_ENABLED=false` по умолчанию |
| [azizmazrou/ews-mcp](https://github.com/azizmazrou/ews-mcp) | EWS | On-prem Exchange | Отправка выключена по умолчанию |
| [Softeria/ms-365-mcp-server](https://github.com/Softeria/ms-365-mcp-server) | Microsoft Graph | Microsoft 365 / Outlook.com | `send-mail` без отдельного подтверждения |
| [elyxlz/microsoft-mcp](https://github.com/elyxlz/microsoft-mcp) | Microsoft Graph | Microsoft 365, несколько аккаунтов | Без отдельного подтверждения |
| [tecnologicachile/mail-mcp](https://mcpservers.org/servers/tecnologicachile/mail-mcp) | IMAP/SMTP, Graph, EWS (OAuth) | Смешанные ящики | Запись включается переменными |
| [agimenez-dev/imap-mcp](https://glama.ai/mcp/servers/agimenezdev/imap-mcp) | IMAP/SMTP | Обычная почта | `mail_send` сначала возвращает превью, отправка с `confirm=true` |

Вывод: для on-prem Exchange уже есть рабочие серверы на exchangelib, и для простого чтения почты их достаточно. Но подтверждение отправки у них либо отсутствует, либо реализовано как «черновик + второй вызов» или `confirm=true`. И то и другое решает сама модель, а значит, его может обойти prompt injection. Этот проект добавляет то, чего не хватает: подтверждение, которое модель нажать не может, с выделением внешних получателей и возможностью править письмо, а также инструменты под конкретные задачи (треды без цитат, статистика отправителей, разбор входящих).

Ещё один путь — MCP elicitation (запрос подтверждения через UI самого клиента, спецификация 2025-06-18). Claude Code её поддерживает, Claude Desktop и многие другие клиенты нет, поэтому здесь используется собственное окно, которое работает с любым клиентом.

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
OWA_MAIL_BACKEND=demo OWA_MAIL_ALLOW_SEND=1 owa-mail-mcp -v   # stdio-сервер на демо-ящике
```

Тесты проходят полный цикл на демо-бэкенде: MCP-клиент → `send_email` → HTTP окна подтверждения → решение пользователя → отправка, а также отказ, тайм-аут, правки, дубликаты, проверки текста и защиту окна от чужих запросов. EWS-бэкенд с живым Exchange в CI не проверяется; первым шагом на реальном сервере запустите `owa-mail-mcp check`.

```
src/owa_mail_mcp/
  server.py          инструменты, инструкции и промпты MCP
  backend.py         интерфейс MailBackend
  ews_backend.py     Exchange через exchangelib
  demo_backend.py    демо-ящик в памяти
  confirm.py         очередь писем, ожидающих решения
  ui.py              локальный HTTP-сервер окна подтверждения
  confirm_page.html  страница окна
  safety.py          проверки исходящих писем, лимит запросов, блокировка после отказа в пароле
  text.py            HTML → текст, отрезание цитат, правила для текста
```

## Лицензия

MIT
