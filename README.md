# AnalyticsBot

Собирает статистику Telegram-каналов через 24 часа после публикации и записывает её в Google Sheets. Поддерживает несколько каналов, альбомы, восстановление после перезапуска и защиту от дублей.

## Готовое приложение

Запустите `AnalyticsBot.exe` на Windows или `AnalyticsBot.app` на macOS и пройдите мастер настройки: Telegram → ключ Google → канал и таблица. Python не нужен. Сервис запускается автоматически; закрытие окна оставляет его в трее или строке меню.

Подробности: [Windows](WINDOWS_README.txt) · [macOS](MACOS_README.txt).

Настройки и авторизация сохраняются:

- Windows — в `user_data` рядом с `.exe`.
- macOS — в `~/Library/Application Support/AnalyticsBot`.

**Не передавайте эти папки клиенту:** они содержат Telegram-сессию и Google-ключ. Для Windows отправляйте `.exe`, `_internal` и инструкцию одной папкой; для Mac — архив `.app`, полученный скриптом сборки.

## Запуск из исходников и сборка

Нужен Python 3.11+. Сборка выполняется на целевой ОС.

Windows:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-gui.txt
.venv\Scripts\python gui_entry.py
```

Сборка: `powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1`. Результат — `gui-dist/AnalyticsBot`.

macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-gui.txt
.venv/bin/python gui_entry.py
```

Сборка: `.venv/bin/python build_macos.py`. Приложение и ZIP появятся в `gui-dist-macos`. Apple Silicon и Intel требуют соответствующих сборок.

## Без интерфейса

Установите `requirements.txt`, скопируйте `.env.example` в `.env`, а `config.example.yaml` в `config.yaml`. Укажите ключи Telegram, JSON-ключ Google, каналы и столбцы. Предоставьте сервисному аккаунту права редактора в таблице.

```bash
python -m analyticsbot --login-qr
python -m analyticsbot
```

Для входа по телефону используйте `--login`. После остановки сервиса можно дозагрузить последние посты старше 24 часов:

```bash
python -m analyticsbot --channel "@example_channel" --last 10
```

CLI и GUI используют отдельные настройки и базы.

## Особенности

- Показатели снимаются однократно после 24 часов. После простоя сбор выполняется позже; задержка более пяти минут отмечается примечанием.
- ER = (реакции + репосты + комментарии) / просмотры; ERR — та же сумма / подписчики. Формулы добавляются в таблицу.
- Сопоставление столбцов настраивается. Сервис оформляет шапку и настроенные столбцы, сохраняя существующие данные и формулы.
- Компьютер должен оставаться включённым и не спать. Очистка таблицы не сбрасывает обработанные задания в SQLite.

Проверки: `python -m unittest discover -s tests -q`.
