import asyncio
import logging
import queue

from PySide6.QtCore import QThread, Signal
from telethon import TelegramClient, errors
from googleapiclient.errors import HttpError

from . import db, sheets
from .main import run


def friendly_error(exc):
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram ограничил частоту запросов. Повторите через {exc.seconds} сек."
    if isinstance(exc, (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError)):
        return "Telegram не принял API ID или API hash. Проверьте данные приложения."
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return "Telegram не принял номер телефона. Укажите его в международном формате."
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return "Код входа устарел. Запросите новый код или войдите по QR-коду."
    if isinstance(exc, (errors.ChannelPrivateError, errors.InviteHashExpiredError, errors.InviteHashInvalidError)):
        return "Нет доступа к каналу. Вступите в него этим Telegram-аккаунтом и проверьте ссылку-приглашение."
    if isinstance(exc, (errors.UsernameInvalidError, errors.UsernameNotOccupiedError)):
        return "Telegram-канал с таким адресом не найден. Скопируйте ссылку из Telegram и проверьте её."
    if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError)):
        return "Сессия Telegram завершена. Откройте подключение Telegram и войдите заново."
    if isinstance(exc, errors.SessionPasswordNeededError):
        return "Требуется пароль двухэтапной защиты Telegram."
    if isinstance(exc, HttpError):
        if exc.resp.status == 403:
            return "Google отклонил доступ. Проверьте Sheets API и доступ сервисного аккаунта к таблице."
        if exc.resp.status == 404:
            return "Таблица Google не найдена. Проверьте ссылку и доступ сервисного аккаунта."
        return f"Ошибка Google Sheets (код {exc.resp.status}). Проверьте таблицу и её вкладку."
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return "Не удалось подключиться или открыть файл. Проверьте интернет и доступ к папке данных."
    if isinstance(exc, RuntimeError) and "Another service instance" in str(exc):
        return "Сервис уже запущен в другом окне или через командную строку. Остановите его."
    if isinstance(exc, RuntimeError) and "Telegram login required" in str(exc):
        return "Сначала войдите в Telegram в настройках приложения."
    if isinstance(exc, ValueError):
        message = str(exc)
        translations = {"channels must be a nonempty list": "Добавьте хотя бы один канал.",
                        "Each field must use a different column": "Для каждого поля укажите свой столбец.",
                        "Invalid sheet.timezone": "Проверьте название часового пояса.",
                        "Telegram login required: run python -m analyticsbot --login first": "Сначала войдите в Telegram в настройках."}
        if "entity" in message.lower() or "no user has" in message.lower():
            return "Канал не найден. Проверьте адрес и доступ аккаунта к каналу."
        return translations.get(message, message)
    return f"Не удалось выполнить операцию: {type(exc).__name__}. Проверьте настройки и подключение."


class GuiLogHandler(logging.Handler):
    def __init__(self, signal):
        super().__init__(logging.INFO)
        self.signal = signal

    def emit(self, record):
        message = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            message = friendly_error(record.exc_info[1])
        elif record.levelno >= logging.ERROR:
            message = "Ошибка сервиса. Проверьте настройки и подключение."
        elif record.name.startswith("telethon"):
            return
        elif message.startswith("Saved "):
            message = "Статистика записана: " + message[len("Saved "):]
        elif message.startswith("Post deleted: "):
            message = "Пост удалён в Telegram: " + message[len("Post deleted: "):]
        self.signal.emit(record.levelno, message)


class ServiceWorker(QThread):
    event = Signal(int, str)
    ready = Signal()
    error = Signal(str)

    def __init__(self, config, backfill_channel=None, backfill_count=None):
        super().__init__()
        self.config = config
        self.backfill_channel = backfill_channel
        self.backfill_count = backfill_count
        self.loop = None
        self.task = None
        self.stop_requested = False

    def run(self):
        handler = GuiLogHandler(self.event)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        old_level = root_logger.level
        root_logger.setLevel(logging.INFO)
        try:
            with db.single_instance(self.config["database"]):
                asyncio.run(self._run_service())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error.emit(friendly_error(exc))
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)
            self.loop = None
            self.task = None

    async def _run_service(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        if self.stop_requested:
            return
        if self.backfill_channel is not None:
            await run(self.config, backfill_channel=self.backfill_channel, backfill_count=self.backfill_count)
        else:
            await run(self.config, on_started=self.ready.emit)

    def stop(self):
        self.stop_requested = True
        if self.loop and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)


class TelegramWorker(QThread):
    stage = Signal(str, str)
    qr_ready = Signal(str)
    result = Signal(bool, str)
    account = Signal(str)

    def __init__(self, api_id, api_hash, session, database, phone=None, logout=False, qr_mode=False):
        super().__init__()
        self.api_id, self.api_hash, self.session, self.database, self.phone = api_id, api_hash, session, database, phone
        self.logout = logout
        self.qr_mode = qr_mode
        self.answers = queue.Queue()
        self.loop = None
        self.task = None

    def answer(self, text):
        self.answers.put(text)

    def cancel(self):
        self.answers.put(None)
        if self.loop and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)

    def run(self):
        try:
            with db.single_instance(self.database):
                asyncio.run(self._work())
        except asyncio.CancelledError:
            self.result.emit(False, "Авторизация отменена.")
        except Exception as exc:
            self.result.emit(False, friendly_error(exc))
        finally:
            self.loop = None
            self.task = None

    async def _work(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        client = TelegramClient(self.session, self.api_id, self.api_hash, flood_sleep_threshold=0)
        try:
            await client.connect()
            if self.logout:
                if await client.is_user_authorized():
                    await client.log_out()
                self.result.emit(True, "Сессия сервиса завершена. Теперь можно войти в другой аккаунт.")
                return
            if await client.is_user_authorized():
                me = await client.get_me()
                self.account.emit(f"{me.first_name or me.id}" + (f" (@{me.username})" if getattr(me, "username", None) else ""))
                self.result.emit(True, f"Telegram подключён: {me.first_name or me.id}")
                return
            if self.qr_mode:
                while True:
                    qr = await client.qr_login()
                    self.qr_ready.emit(qr.url)
                    self.stage.emit("qr", "Отсканируйте QR-код в Telegram: Настройки → Устройства → Подключить устройство.")
                    try:
                        me = await qr.wait()
                        break
                    except asyncio.TimeoutError:
                        continue
                    except errors.SessionPasswordNeededError:
                        if not await self._password(client):
                            return
                        me = await client.get_me()
                        break
                    except Exception:
                        if not await client.is_user_authorized():
                            raise
                        me = await client.get_me()
                        if not me:
                            raise
                        break
                if not me:
                    me = await client.get_me()
                self.account.emit(f"{me.first_name or me.id}" + (f" (@{me.username})" if getattr(me, "username", None) else ""))
                self.result.emit(True, f"Telegram подключён: {me.first_name or me.id}")
                return
            if not self.phone:
                self.result.emit(False, "Telegram ещё не авторизован. Введите номер и запросите код.")
                return
            sent = await client.send_code_request(self.phone)
            method = type(sent.type).__name__
            delivery = {"SentCodeTypeApp": "Проверьте служебный чат Telegram на другом устройстве.",
                        "SentCodeTypeSms": "Проверьте SMS.", "SentCodeTypeEmailCode": "Проверьте почту для входа."}.get(method, "Проверьте сообщение от Telegram.")
            while True:
                self.stage.emit("code", delivery)
                code = await asyncio.to_thread(self.answers.get)
                if code is None:
                    return
                try:
                    await client.sign_in(self.phone, code=code)
                    break
                except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                    delivery = "Неверный код. Проверьте его и попробуйте ещё раз."
                except errors.SessionPasswordNeededError:
                    if not await self._password(client):
                        return
                    break
            me = await client.get_me()
            self.account.emit(f"{me.first_name or me.id}" + (f" (@{me.username})" if getattr(me, "username", None) else ""))
            self.result.emit(True, f"Telegram подключён: {me.first_name or me.id}")
        finally:
            await client.disconnect()

    async def _password(self, client):
        message = "Введите пароль двухэтапной защиты Telegram."
        while True:
            self.stage.emit("password", message)
            password = await asyncio.to_thread(self.answers.get)
            if password is None:
                return False
            try:
                await client.sign_in(password=password)
                return True
            except errors.PasswordHashInvalidError:
                message = "Неверный пароль. Попробуйте ещё раз."


class GoogleWorker(QThread):
    result = Signal(bool, str)
    tabs = Signal(list)

    def __init__(self, credentials, channel=None):
        super().__init__()
        self.credentials = credentials
        self.channel = channel

    def run(self):
        try:
            api = sheets.create_api(self.credentials)
            if self.channel:
                target = self.channel["sheet"]
                result = api.spreadsheets().get(spreadsheetId=target["spreadsheet_id"], fields="sheets.properties.title").execute()
                names = [item["properties"]["title"] for item in result["sheets"]]
                self.tabs.emit(names)
                if target.get("worksheet") and target["worksheet"] not in names:
                    raise ValueError("Вкладка не найдена. Проверьте её название в настройках канала.")
                self.result.emit(True, "Google Sheets подключён, вкладка найдена.")
            else:
                raise ValueError("Добавьте канал с таблицей для проверки доступа.")
        except Exception as exc:
            self.result.emit(False, friendly_error(exc))


class ChannelCheckWorker(QThread):
    result = Signal(bool, str)

    def __init__(self, config, channel):
        super().__init__()
        self.config = config
        self.channel = channel

    def run(self):
        try:
            with db.single_instance(self.config["database"]):
                asyncio.run(self.check())
            self.result.emit(True, "Telegram-канал и выбранная вкладка Google Sheets доступны.")
        except Exception as exc:
            self.result.emit(False, friendly_error(exc))

    async def check(self):
        client = TelegramClient(self.config["session"], self.config["api_id"], self.config["api_hash"], flood_sleep_threshold=0)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Сессия Telegram завершена. Вернитесь к подключению Telegram и войдите снова.")
            source = self.channel["source"]
            lookup = int(source) if str(source).lstrip("-").isdigit() else source
            if isinstance(lookup, int):
                await client.get_dialogs()
            entity = await client.get_entity(lookup)
            if not getattr(entity, "broadcast", False):
                raise ValueError("Это не Telegram-канал. Укажите адрес канала, а не пользователя или группы.")
            await client.get_messages(entity, limit=1)
        finally:
            await client.disconnect()
        await asyncio.to_thread(self.check_sheet)

    def check_sheet(self):
        target = self.channel["sheet"]
        api = sheets.create_api(self.config["credentials"])
        result = api.spreadsheets().get(spreadsheetId=target["spreadsheet_id"], fields="sheets.properties(title,gridProperties.columnCount)").execute()
        sheet = next((item["properties"] for item in result["sheets"] if item["properties"]["title"] == target["worksheet"]), None)
        if sheet is None:
            raise ValueError("Вкладка больше не существует. Обновите список вкладок и выберите её снова.")
        if max(map(sheets.column_index, target["columns"].values())) >= sheet["gridProperties"]["columnCount"]:
            raise ValueError("В таблице недостаточно столбцов. Добавьте недостающие столбцы или измените сопоставление.")
