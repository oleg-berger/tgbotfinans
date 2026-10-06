# Бот на Cloudflare Workers

Для пользователя сохранены прежние команды, кнопки, семь страниц гайда с изображениями, банки и основной банк, категории и ключи, быстрые записи и неизвестные слова, доходы и расходы, начальные остатки, переводы и сверка, редактирование, удаление и архивирование, ставки и выплаты кэшбэка, статистика и CSV, часовой пояс и оба вида напоминаний. Диалоги сохраняются при перезапусках. Предложение первого кэшбэка появляется после гайда и создания первого банка.

## Как устроена версия

```text
Telegram webhook → frontend.js (без проверки секрета webhook)
                 → FinanceStore в tgbotfinans-core (Python Durable Object)
                 → Application → Session / Views → Ledger → SQLite
                 → сохраненный ответ / очередь доставки → Telegram Bot API
Cron каждую минуту → тот же FinanceStore → напоминания и резервная копия
```

Публикуются два Worker: публичный `tgbotfinans` и внутренний `tgbotfinans-core`. У внутреннего отключены workers.dev и preview URLs; его обычный HTTP-обработчик возвращает 404. Доступ к `FinanceStore` идет через binding из публичного Worker. Один объект с именем `finance-v1` содержит всех пользователей, как прежняя SQLite-база, и сохраняет идентификаторы при переносе.

Публичный обработчик написан на JavaScript, чтобы укладываться в лимит CPU обычного Worker Free. Python исполняется внутри Durable Object. `transactionSync` обеспечивает атомарное сохранение финансового действия, диалога, события и очереди ответа; вложенная транзакция заменяет SAVEPOINT. Отправка в Telegram выполняется после commit. Повторный webhook не создает повторную операцию. При временном сбое доставки ответ сохраняется и повторяется через Cron; следующие действия этого пользователя ждут доставки предыдущего ответа.

Сбой между отправкой ответа и отметкой доставки может повторить сообщение, но не финансовую операцию. Такой же предел есть у напоминаний. Для HTTP 403 от Telegram доставка прекращается: пользователь мог заблокировать бота.

`Application`, `Ledger`, парсер, `Session`, `Views`, `Screen` и гайд общие с локальным запуском. Миграции 1–4 перенесены без изменения в `financebot/schema.py`; версия 5 добавляет индексы дат для минутной очистки без полного сканирования истории и лишнего расхода квот. Деньги остаются целыми тиынами. Суммарные остатки считаются целыми Python, чтобы SQL → JavaScript не округлял большие суммы выше 2⁵³.

## Подготовка на Windows или Linux

Нужны [Node.js](https://nodejs.org/) 22 или новее, npm, [uv](https://docs.astral.sh/uv/getting-started/installation/) и аккаунт Cloudflare. Выполняйте команды из `cloudflare/`:

```text
uv run --python 3.13 --locked python build.py --vendor
npm ci
npm run check
```

`build.py` копирует только перечисленные модули и семь PNG, а `--vendor` добавляет закрепленные SDK и tzdata из окружения uv. Рабочая `.env`, базы и резервные копии в сборку не включаются. Используется непосредственный Wrangler с подготовленными Python-модулями: сборка работает на Windows без запуска WASM-интерпретатора через pywrangler.

`npm run check` выполняет `deploy --dry-run` для обоих Worker без публикации. PNG находятся в Static Assets и не увеличивают Python bundle. После изменения общих исходников или иллюстраций снова выполните `build.py`. Перед обновлением SDK остановите `wrangler dev`, затем пересоберите vendor.

Имена Worker можно поменять в TOML; при переименовании core одновременно поменяйте `script_name` у binding `FINANCE` во frontend. Имя объекта `finance-v1`, класс `FinanceStore` и уже примененную миграцию `v1` после начала эксплуатации не меняйте: они определяют расположение данных.

## Публикация

### Через Git и Cloudflare Dashboard

Для Workers Builds настройте два Worker, подключенные к одному репозиторию и ветке `codex/cloudflare-workers`. Корневая папка сборки — **`cloudflare`**, а не корень репозитория. Исходники этой ветки и следующие изменения должны быть закоммичены и отправлены в Git перед сборкой.

| Поле | Внутренний Worker | Публичный Worker |
| --- | --- | --- |
| Worker name | `tgbotfinans-core` | `tgbotfinans` |
| Production branch | `codex/cloudflare-workers` | `codex/cloudflare-workers` |
| Root directory | `cloudflare` | `cloudflare` |
| Build command | `npm run build` | `npm run build` |
| Deploy command | `npm run deploy:core` | `npm run deploy:frontend` |

Сначала успешно опубликуйте `tgbotfinans-core`, затем запускайте сборку `tgbotfinans`: его binding ссылается на уже опубликованный core. Cloudflare устанавливает npm-зависимости по `package-lock.json`; команда `build` устанавливает закрепленный uv и готовит Python-модули, SDK, tzdata и изображения. В поле Deploy command укажите команду из соответствующего столбца. Команда `npm run deploy`, публикующая оба Worker подряд, предназначена для локального терминала: в Workers Builds используйте отдельную команду для каждого Worker. [Настройки сборки](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/) и [несколько Worker в одном репозитории](https://developers.cloudflare.com/workers/ci-cd/builds/advanced-setups/) описаны в документации Cloudflare.

Если включены сборки других веток, задайте их Preview/Non-production deploy command явно: `npx wrangler deploy --dry-run -c wrangler.core.toml` для core и `npx wrangler deploy --dry-run -c wrangler.toml` для frontend. Так проверка ветки не заменит рабочий Durable Object. Для первого запуска достаточно сборок production-ветки.

После публикации в **Settings → Runtime variables and secrets** задайте runtime-секрет `BOT_TOKEN` у `tgbotfinans-core`, а `ADMIN_SECRET` — у `tgbotfinans` для администрирования. `WEBHOOK_SECRET` больше не используется. Build variables доступны только во время сборки и не заменяют runtime-секреты. Далее выполните перенос данных и `manage.py configure`, как описано ниже.

По запросу владельца `/webhook` принимает POST без проверки секрета. Зная публичный адрес и Telegram ID пользователя, посторонний может подделать события и изменить его данные. `/admin/` по-прежнему требует `ADMIN_SECRET`; core остается приватным.

Ошибка `Could not detect a directory containing static files` при установке корневого `requirements.txt` означает, что Wrangler запущен без конфигурации из папки `cloudflare` или из ветки без этой версии. Проверьте Root directory, Production branch и наличие файлов `cloudflare/package.json`, `cloudflare/wrangler.toml`, `cloudflare/wrangler.core.toml` в удаленном репозитории. Создавать HTML-страницу для исправления этой ошибки не требуется.

### Из локального терминала

Эти команды изменяют ваш аккаунт Cloudflare; используйте основной `wrangler.toml` и `wrangler.core.toml`:

```text
npx wrangler login
npm run deploy
npx wrangler secret put BOT_TOKEN -c wrangler.core.toml
npx wrangler secret put ADMIN_SECRET
```

Wrangler сначала публикует core с SQLite Durable Object, затем frontend. `BOT_TOKEN` — токен BotFather, `ADMIN_SECRET` — длинный случайный секрет администрирования. Ввод производится по запросу Wrangler; токены не записываются в TOML. Без `ADMIN_SECRET` frontend отклоняет администрирование; для webhook секрет не нужен. Сохраните секреты в менеджере паролей.

Публичный адрес будет вида `https://tgbotfinans.<ваш-поддомен>.workers.dev`. Откройте `/health`: он должен ответить `{"status":"ok"}`. Эта проверка подтверждает доступность frontend; доступ к базе проверяется командой `backups` ниже.

Если есть старая база, **сначала перенесите ее** по следующему разделу. Затем остановите прежний polling-экземпляр и настройте Telegram:

```text
uv run --python 3.13 --locked python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev configure
uv run --python 3.13 --locked python manage.py telegram-status
```

`configure` запросит только токен, установит прежние 12 команд, webhook `/webhook` без секрета, только `message`/`callback_query` и `max_connections=1`. Ожидающие обновления не удаляются. `manage.py --url АДРЕС probe-webhook` проверяет обработку пустого POST без создания финансовой операции. Отправьте `/start`, пройдите гайд, создайте банк и проверьте запись, статистику и CSV в Telegram. Проверка реальным Telegram и фактических лимитов аккаунта выполняется после публикации; локальные тесты не заменяют ее.

## Перенос существующих данных

Остановите старый экземпляр бота. Из корня проекта используйте существующее Python-окружение:

```powershell
.\.venv\Scripts\python.exe cloudflare/manage.py export-sqlite data/finance.sqlite3 transfer/finance.json
```

На Linux интерпретатор — `.venv/bin/python`. Укажите фактический путь явно. Утилита читает SQLite через Backup API, проверяет целостность и схему, обновляет при необходимости только временную копию в памяти, не изменяет исходную базу и не перезаписывает существующий файл. Переносятся все пользователи, идентификаторы, банки, категории, операции, ставки, диалоги, ответы событий и отметки напоминаний.

Из `cloudflare/` загрузите снимок **в пустую** базу Worker:

```text
uv run --python 3.13 --locked python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev import ../transfer/finance.json
```

Введите `ADMIN_SECRET`. Импорт проверяет формат, таблицы, столбцы и ссылки и выполняется одной транзакцией. При ошибке все изменения откатываются. Повторный импорт в непустую базу отклоняется. Предел одного снимка/запроса — 20 МиБ; для текущего небольшого бота этого достаточно. Снимок содержит финансовые данные: `transfer/` исключена из Git. Не передавайте файл в сообщения или публичное хранилище.

При возврате с Cloudflare сначала удалите webhook (`manage.py disconnect`), затем запустите один локальный экземпляр. Старая локальная база не содержит новые облачные операции. Для возврата с актуальными данными сначала экспортируйте облачный снимок и преобразуйте его в отдельную SQLite-базу командой `snapshot-to-sqlite`, описанной ниже.

```text
uv run python manage.py snapshot-to-sqlite ../transfer/cloud.json ../transfer/restored.sqlite3
```

Проверяются схема, ссылки и целостность; существующая база никогда не перезаписывается. Укажите новую базу в локальной конфигурации. Перед окончательным экспортом отключите Cron в конфигурации frontend (`crons = []` и публикация frontend), удалите webhook и дождитесь завершения текущих запросов, чтобы облако больше не изменяло данные и не дублировало напоминания.

## Резервные копии и администрирование

Раз в UTC-день Cron сохраняет согласованный JSON-снимок в том же Durable Object; хранятся семь ежедневных копий. Ручные и `before-reset-*` / `before-restore-*` автоматически не удаляются. Поэтому скачивайте копии на свой компьютер: удаление namespace уничтожит и данные, и эти копии. Сбой копирования не останавливает напоминания; следующая минута повторит попытку.

Все административные обращения — POST с Bearer `ADMIN_SECRET`. Скрипт спрашивает секрет, не выводит финансовые строки в консоль и не читает `.env`. Команды из `cloudflare/`:

```text
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev backup
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev backups
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev export ../transfer/cloud.json
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev download ИМЯ-КОПИИ ../transfer/backup.json
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev restore ИМЯ-КОПИИ --confirm
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev reset-user TELEGRAM_ID
uv run python manage.py --url https://tgbotfinans.ВАШ-ПОДДОМЕН.workers.dev reset-user TELEGRAM_ID --confirm
```

`reset-user` без `--confirm` только показывает количества записей. Подтвержденный сброс и восстановление сначала создают полную копию; при ошибке копирования удаление не начинается. Сброс сохраняет других пользователей. Восстановление заменяет всю финансовую базу, очищает очередь старых ответов и отметки доставки. SQLite и очередь изменяются под общей блокировкой.

## Проверки

Из корня проекта:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q --cloud-storage tests/test_finance.py tests/test_application.py tests/test_guide.py
.\.venv\Scripts\python.exe -m compileall -q financebot cloudflare/build.py cloudflare/manage.py cloudflare/entry.py
```

Вторая команда запускает прежние финансовые сценарии через новый адаптер транзакций, SQLite-имитацию API Durable Object и тестовые базы. Дополнительные тесты проверяют повторную доставку, rollback, резервные копии, восстановление, сброс и CLI переноса.

Приемка в настоящем локальном workerd, с синтетическими данными и внутренним Telegram stub:

```text
# Из cloudflare/, в первом терминале:
npm run test:runtime
# Во втором терминале:
uv run python test/smoke.py
```

Тест проверяет авторизацию, PNG и multipart, семь страниц, создание первого банка, первый кэшбэк, начальный остаток в доходах, расход, выплату, CSV, редактирование, удаление, повтор webhook, изоляцию, копирование, сброс, восстановление и Cron. Тестовые конфигурации `wrangler*.test.toml` и `test/telegram.toml` используются только локально. Они никогда не должны публиковаться: в них открытые тестовые секреты. Локальное тестовое состояние находится в `.wrangler/test-state`, отдельно от production.

## Бесплатный тариф

По [документации Cloudflare](https://developers.cloudflare.com/durable-objects/platform/pricing/) SQLite Durable Objects доступны на Workers Free: 100 000 запросов/день, 13 000 GB-s/день, 5 млн прочитанных и 100 000 записанных строк/день, 5 GB хранения на аккаунт. [Лимит одного объекта](https://developers.cloudflare.com/durable-objects/platform/limits/) на Free — 1 GB. Cron дает примерно 1440 обращений к объекту в сутки. Резервные копии и служебные KV-записи тоже входят в квоты.

Для нескольких пользователей ожидается небольшой расход квот, но фактическое потребление проверяется в Cloudflare Dashboard после запуска. Оставайтесь на Free: при превышении его лимитов операции ограничиваются ошибкой; платный тариф проект не включает. Платный VPS, D1, R2 и внешний PostgreSQL этой реализации не нужны. Тарифные лимиты могут меняться — перед публикацией проверяйте связанные официальные страницы.
