import copy
import re
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QProgressBar, QPushButton, QScrollArea,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
                               QHeaderView)
from google.oauth2.service_account import Credentials

from .gui_config import DEFAULT_COLUMNS, channel_source, runtime_config, save_settings, spreadsheet_id
from .gui_workers import ChannelCheckWorker, GoogleWorker, TelegramWorker, friendly_error
from .sheets import HEADERS


def text(value, muted=False):
    item = QLabel(value)
    item.setWordWrap(True)
    if muted:
        item.setObjectName("subtitle")
    return item


def link(title, url):
    item = text(f'<a href="{url}" style="color:#828fff">{title}</a>')
    item.setOpenExternalLinks(True)
    return item


def action(title, callback, primary=False):
    item = QPushButton(title)
    if primary:
        item.setObjectName("primary")
    item.clicked.connect(callback)
    return item


def persist(root, settings):
    save_settings(root / "settings.json", settings)


class TelegramPanel(QWidget):
    changed = Signal()

    def __init__(self, root, settings):
        super().__init__()
        self.root, self.settings = root, settings
        self.worker = None
        self.operation = ""
        if not (root / "telegram.session").is_file():
            settings.update(telegram_verified=False, channel_verified=False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(text("Получите API ID и API Hash и укажите их ниже", True))
        self.help_toggle = QToolButton()
        self.help_toggle.setText("Как получить?")
        self.help_toggle.setCheckable(True)
        self.help_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.help_toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.help_toggle)
        self.help_text = text(
            '1. Откройте <a href="https://my.telegram.org/apps" style="color:#828fff">my.telegram.org/apps</a> '
            'и войдите по номеру телефона и коду из Telegram.<br>'
            '2. Откройте API development tools. Если приложение уже создано, используйте показанные ключи.<br>'
            '3. Для нового приложения укажите App title (например, AnalyticsBot) и Short name '
            '(например, analyticsbot). В Platform выберите Desktop; URL можно оставить пустым, '
            'в Description при необходимости кратко опишите приложение.<br>'
            '4. Нажмите Create application и скопируйте App api_id и App api_hash в поля ниже. '
            'API Hash никому не сообщайте.', True)
        self.help_text.setOpenExternalLinks(True)
        self.help_text.hide()
        self.help_toggle.toggled.connect(self.help_text.setVisible)
        self.help_toggle.toggled.connect(lambda opened: self.help_toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
        layout.addWidget(self.help_text)
        form = QFormLayout()
        form.setVerticalSpacing(8)
        self.api_id = QLineEdit(str(settings.get("api_id", "")))
        self.api_hash = QLineEdit(settings.get("api_hash", ""))
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone = QLineEdit(settings.get("phone", ""))
        self.phone.setPlaceholderText("+7…")
        form.addRow("API ID", self.api_id)
        form.addRow("API hash", self.api_hash)
        form.addRow("Телефон", self.phone)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.phone_button = action("Получить код", lambda: self.begin("phone"), True)
        self.qr_button = action("Войти по QR", lambda: self.begin("qr"))
        self.check_button = action("Проверить подключение", lambda: self.begin("check"))
        row.addWidget(self.phone_button)
        row.addWidget(self.qr_button)
        row.addWidget(self.check_button)
        row.addStretch()
        layout.addLayout(row)
        self.code_box = QWidget()
        code_row = QHBoxLayout(self.code_box)
        code_row.setContentsMargins(0, 0, 0, 0)
        self.code = QLineEdit()
        self.code.setPlaceholderText("Код из сообщения Telegram")
        self.code_submit = action("Подтвердить код", lambda: self.answer(self.code))
        code_row.addWidget(self.code)
        code_row.addWidget(self.code_submit)
        layout.addWidget(self.code_box)
        self.code_box.hide()
        self.password_box = QWidget()
        password_row = QHBoxLayout(self.password_box)
        password_row.setContentsMargins(0, 0, 0, 0)
        self.password = QLineEdit()
        self.password.setPlaceholderText("Пароль двухэтапной защиты Telegram")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_submit = action("Подтвердить пароль", lambda: self.answer(self.password))
        password_row.addWidget(self.password)
        password_row.addWidget(self.password_submit)
        layout.addWidget(self.password_box)
        self.password_box.hide()
        self.qr_image = QLabel()
        self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr_image)
        self.qr_image.hide()
        self.account = text("")
        layout.addWidget(self.account)
        self.status = text("", True)
        self.status.hide()
        layout.addWidget(self.status)
        last = QHBoxLayout()
        self.logout_button = action("Войти в другой аккаунт", lambda: self.begin("logout"))
        self.cancel_button = action("Отменить вход", self.cancel)
        self.cancel_button.hide()
        last.addWidget(self.logout_button)
        last.addWidget(self.cancel_button)
        last.addStretch()
        layout.addLayout(last)
        layout.addStretch()
        self.api_id.textChanged.connect(self.credentials_changed)
        self.api_hash.textChanged.connect(self.credentials_changed)
        self.phone.textChanged.connect(self.phone_changed)
        self.refresh_account()

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def credentials_changed(self):
        self.settings.update(api_id=self.api_id.text().strip(), api_hash=self.api_hash.text().strip(), telegram_verified=False, channel_verified=False)
        self.refresh_account()
        self.set_status("")
        persist(self.root, self.settings)
        self.changed.emit()

    def phone_changed(self):
        self.settings["phone"] = self.phone.text().strip()
        persist(self.root, self.settings)

    def refresh_account(self):
        verified = self.settings.get("telegram_verified", False)
        self.account.setText("Подключён: " + self.settings.get("telegram_account", "Telegram") if verified else "")
        self.account.setVisible(verified)
        self.logout_button.setVisible(verified)
        self.phone_button.setObjectName("" if verified else "primary")
        self.phone_button.style().unpolish(self.phone_button)
        self.phone_button.style().polish(self.phone_button)

    def set_status(self, message):
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def begin(self, operation):
        if self.busy():
            return
        try:
            api_id = int(self.api_id.text())
            if api_id <= 0 or not self.api_hash.text().strip():
                raise ValueError("Укажите API ID и API Hash.")
            if operation == "phone" and not re.fullmatch(r"\+[0-9]{7,15}", self.phone.text().strip()):
                raise ValueError("Введите номер с кодом страны, например +7…")
        except (ValueError, TypeError) as exc:
            self.set_status(str(exc) if not str(exc).startswith("invalid literal") else "API ID должен состоять из цифр.")
            return
        self.operation = operation
        self.settings.update(api_id=self.api_id.text().strip(), api_hash=self.api_hash.text().strip(), phone=self.phone.text().strip())
        if operation == "logout":
            self.settings.update(telegram_verified=False, channel_verified=False, telegram_account="")
        persist(self.root, self.settings)
        self.worker = TelegramWorker(api_id, self.api_hash.text().strip(), str(self.root / "telegram"), str(self.root / "jobs.sqlite3"),
                                     self.phone.text().strip() if operation == "phone" else None, operation == "logout", operation == "qr")
        self.worker.account.connect(self.account_received)
        self.worker.stage.connect(self.stage)
        self.worker.qr_ready.connect(self.show_qr)
        self.worker.result.connect(self.result)
        self.worker.finished.connect(self.finished)
        self.set_busy(True)
        self.set_status("Подключаемся…")
        self.worker.start()
        self.changed.emit()

    def set_busy(self, busy):
        for widget in (self.api_id, self.api_hash, self.phone, self.phone_button, self.qr_button, self.check_button, self.logout_button):
            widget.setEnabled(not busy)
        self.cancel_button.setVisible(busy)

    def account_received(self, account):
        self.settings["telegram_account"] = account

    def stage(self, stage, message):
        self.set_status(message)
        self.code_box.setVisible(stage == "code")
        self.password_box.setVisible(stage == "password")
        self.qr_image.setVisible(stage == "qr")
        self.code_submit.setEnabled(stage == "code")
        self.password_submit.setEnabled(stage == "password")
        if stage in ("code", "password"):
            field = self.code if stage == "code" else self.password
            field.setFocus()
            QTimer.singleShot(0, lambda: self.reveal(field))
        elif stage == "qr":
            QTimer.singleShot(0, lambda: self.reveal(self.status))

    def reveal(self, widget):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(widget, 0, 12)
                return
            parent = parent.parentWidget()

    def show_qr(self, url):
        import qrcode
        code = qrcode.QRCode(border=4)
        code.add_data(url)
        matrix = code.get_matrix()
        size = len(matrix)
        image = QImage(size, size, QImage.Format.Format_RGB32)
        for y, row in enumerate(matrix):
            for x, value in enumerate(row):
                image.setPixelColor(x, y, QColor("black" if value else "white"))
        self.qr_image.setPixmap(QPixmap.fromImage(image).scaled(230, 230, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        self.qr_image.show()
        QTimer.singleShot(0, lambda: self.reveal(self.qr_image))

    def answer(self, field):
        if self.busy() and field.text().strip():
            self.worker.answer(field.text().strip() if field is self.code else field.text())
            field.clear()
            self.code_submit.setEnabled(False)
            self.password_submit.setEnabled(False)

    def result(self, success, message):
        self.settings["telegram_verified"] = success and self.operation != "logout"
        if not self.settings["telegram_verified"]:
            self.settings["channel_verified"] = False
        self.set_status("" if self.settings["telegram_verified"] else message)
        self.refresh_account()
        persist(self.root, self.settings)
        self.changed.emit()

    def finished(self):
        self.set_busy(False)
        self.code_box.hide()
        self.password_box.hide()
        self.qr_image.hide()
        self.code.clear()
        self.password.clear()
        self.changed.emit()

    def cancel(self):
        if self.busy():
            self.worker.cancel()


class GooglePanel(QWidget):
    changed = Signal()

    def __init__(self, root, settings, key_name="google-service-account.json", allow_check=False):
        super().__init__()
        self.root, self.settings, self.key_name = root, settings, key_name
        self.worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(text("Для записи в таблицу нужен JSON-ключ сервисного аккаунта Google."))
        self.help_toggle = QToolButton()
        self.help_toggle.setText("Как получить?")
        self.help_toggle.setCheckable(True)
        self.help_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.help_toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.help_toggle)
        self.help_text = text(
            '1. Откройте <a href="https://console.cloud.google.com/iam-admin/serviceaccounts" style="color:#828fff">сервисные аккаунты Google Cloud</a>, выберите или создайте проект и включите Google Sheets API.<br>'
            '2. Создайте сервисный аккаунт.<br>'
            '3. Откройте его → Ключи → Добавить ключ → Создать новый ключ → JSON.', True)
        self.help_text.setOpenExternalLinks(True)
        self.help_text.hide()
        self.help_toggle.toggled.connect(self.help_text.setVisible)
        self.help_toggle.toggled.connect(lambda opened: self.help_toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
        layout.addWidget(self.help_text)
        row = QHBoxLayout()
        self.choose_button = action("Выбрать JSON-файл", self.choose, True)
        row.addWidget(self.choose_button)
        row.addStretch()
        layout.addLayout(row)
        self.email = text("")
        self.email.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.email)
        layout.addWidget(text("В Google-таблице нажмите «Настройки доступа», добавьте этот email и выдайте роль «Редактор».", True))
        layout.addWidget(link("Создать новую Google-таблицу", "https://docs.google.com/spreadsheets/create"))
        self.status = text("Доступ к конкретной таблице проверим после выбора канала и вкладки.", True)
        layout.addWidget(self.status)
        self.check_table = QLineEdit(settings["channels"][0]["spreadsheet"] if settings.get("channels") else "")
        self.check_table.setPlaceholderText("Ссылка на Google-таблицу для проверки нового ключа")
        self.check_table.setVisible(allow_check)
        layout.addWidget(self.check_table)
        self.check_button = action("Проверить доступ к таблице", self.check)
        self.check_button.setVisible(allow_check)
        layout.addWidget(self.check_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        if settings.get("credentials") and not settings.get("google_email"):
            try:
                settings["google_email"] = Credentials.from_service_account_file(str(root / settings["credentials"])).service_account_email
            except Exception:
                pass
        self.refresh()

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def ready(self):
        return bool(self.settings.get("credentials") and self.settings.get("google_email") and (self.root / self.settings["credentials"]).is_file())

    def refresh(self):
        self.email.setText(self.settings.get("google_email") or "JSON-ключ пока не выбран")

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "JSON-ключ сервисного аккаунта", "", "JSON (*.json)")
        if not path:
            return
        try:
            credentials = Credentials.from_service_account_file(path)
            if not credentials.service_account_email:
                raise ValueError("В файле нет email сервисного аккаунта.")
            destination = self.root / self.key_name
            if Path(path).resolve() != destination.resolve():
                temporary = destination.with_suffix(".tmp")
                shutil.copy2(path, temporary)
                temporary.replace(destination)
            self.settings.update(credentials=self.key_name, google_email=credentials.service_account_email, google_verified=False, channel_verified=False)
            self.refresh()
            self.status.setText("Ключ выбран. Предоставьте этому email права редактора в вашей таблице.")
            self.changed.emit()
        except Exception:
            self.status.setText("Это не JSON-ключ сервисного аккаунта. Скачайте ключ в Google Cloud: сервисный аккаунт → Ключи → JSON.")

    def check(self):
        if self.busy():
            return
        if not self.ready():
            self.status.setText("Сначала выберите JSON-ключ сервисного аккаунта.")
            return
        try:
            target = {"spreadsheet_id": spreadsheet_id(self.check_table.text()), "worksheet": ""}
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.worker = GoogleWorker(str(self.root / self.settings["credentials"]), {"sheet": target})
        self.worker.result.connect(self.result)
        self.worker.finished.connect(self.finished)
        self.choose_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.check_table.setEnabled(False)
        self.status.setText("Проверяем доступ к таблице…")
        self.worker.start()
        self.changed.emit()

    def result(self, success, message):
        self.settings["google_verified"] = success
        self.status.setText(message)
        self.changed.emit()

    def finished(self):
        self.choose_button.setEnabled(True)
        self.check_button.setEnabled(True)
        self.check_table.setEnabled(True)
        self.changed.emit()

    def cancel(self):
        pass


class ChannelPanel(QWidget):
    changed = Signal()

    def __init__(self, root, settings, channel=None, save_draft=False):
        super().__init__()
        self.root, self.settings, self.save_draft = root, settings, save_draft
        self.worker = None
        self.checked = False
        item = channel or settings.get("setup_channel") or (settings["channels"][0] if settings.get("channels") else {"source": "", "spreadsheet": "", "worksheet": "", "columns": DEFAULT_COLUMNS.copy()})
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.inputs = QWidget()
        form = QFormLayout(self.inputs)
        form.setContentsMargins(0, 0, 0, 0)
        self.source = QLineEdit(str(item["source"]))
        self.source.setPlaceholderText("https://t.me/channel или @channel")
        self.spreadsheet = QLineEdit(item["spreadsheet"])
        self.spreadsheet.setPlaceholderText("https://docs.google.com/spreadsheets/d/…")
        self.worksheet = QComboBox()
        self.worksheet.setPlaceholderText("Сначала загрузите вкладки")
        if item.get("worksheet"):
            self.worksheet.addItem(item["worksheet"])
            self.worksheet.setCurrentIndex(0)
        form.addRow("Telegram-канал", self.source)
        form.addRow("Google-таблица", self.spreadsheet)
        tabs_row = QHBoxLayout()
        tabs_row.addWidget(self.worksheet, 1)
        self.tabs_button = action("Загрузить вкладки", self.load_tabs)
        tabs_row.addWidget(self.tabs_button)
        form.addRow("Вкладка", tabs_row)
        layout.addWidget(self.inputs)
        layout.addWidget(text("Аккаунт Telegram должен иметь доступ к каналу. Для приватного канала сначала вступите в него. Вкладка выбирается из существующих в Google-таблице.", True))
        self.columns_toggle = QToolButton()
        self.columns_toggle.setText("Сопоставление столбцов")
        self.columns_toggle.setCheckable(True)
        self.columns_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.columns_toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.columns_toggle)
        self.columns = QTableWidget(len(DEFAULT_COLUMNS), 2)
        self.columns.setHorizontalHeaderLabels(["Поле", "Столбец"])
        self.columns.verticalHeader().setVisible(False)
        self.columns.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.columns.setMaximumHeight(230)
        for row, field in enumerate(DEFAULT_COLUMNS):
            title = QTableWidgetItem(HEADERS[field])
            title.setFlags(title.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.columns.setItem(row, 0, title)
            self.columns.setItem(row, 1, QTableWidgetItem(item["columns"].get(field, DEFAULT_COLUMNS[field])))
        self.columns.hide()
        self.columns_toggle.toggled.connect(self.toggle_columns)
        layout.addWidget(self.columns)
        row = QHBoxLayout()
        self.check_button = action("Проверить канал и таблицу", self.verify, True)
        row.addWidget(self.check_button)
        row.addStretch()
        layout.addLayout(row)
        self.status = text("После заполнения полей проверьте оба подключения.", True)
        layout.addWidget(self.status)
        layout.addStretch()
        self.source.textChanged.connect(self.invalidate)
        self.spreadsheet.textChanged.connect(self.sheet_changed)
        self.worksheet.currentTextChanged.connect(self.invalidate)
        self.columns.itemChanged.connect(self.invalidate)
        self.checked = bool(save_draft and settings.get("channel_verified") and settings.get("setup_checked_channel") == self.value())
        if self.checked:
            self.status.setText("Канал и таблица проверены.")

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def toggle_columns(self, opened):
        self.columns.setVisible(opened)
        self.columns_toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)

    def value(self):
        return {"source": self.source.text().strip(), "spreadsheet": self.spreadsheet.text().strip(), "worksheet": self.worksheet.currentText(),
                "columns": {field: self.columns.item(row, 1).text().strip().upper() if self.columns.item(row, 1) else "" for row, field in enumerate(DEFAULT_COLUMNS)}}

    def validated_value(self):
        item = self.value()
        channel_source(item["source"])
        spreadsheet_id(item["spreadsheet"])
        if not item["worksheet"]:
            raise ValueError("Загрузите список вкладок и выберите нужную.")
        columns = list(item["columns"].values())
        if any(not re.fullmatch(r"[A-Z]{1,3}", value) for value in columns) or len(set(columns)) != len(columns):
            raise ValueError("Укажите разные буквы столбцов, например A или AA.")
        return item

    def invalidate(self, *args):
        self.checked = False
        self.status.setText("Настройки изменены. Проверьте канал и таблицу.")
        if self.save_draft:
            self.settings.update(setup_channel=self.value(), channel_verified=False)
        self.changed.emit()

    def sheet_changed(self):
        self.worksheet.clear()
        self.invalidate()

    def load_tabs(self):
        if self.busy():
            return
        try:
            identity = spreadsheet_id(self.spreadsheet.text())
            credentials = self.root / self.settings.get("credentials", "")
            if not credentials.is_file():
                raise ValueError("Сначала выберите JSON-ключ Google.")
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.worker = GoogleWorker(str(credentials), {"sheet": {"spreadsheet_id": identity, "worksheet": ""}})
        self.worker.tabs.connect(self.tabs_loaded)
        self.worker.result.connect(self.tabs_result)
        self.worker.finished.connect(self.finished)
        self.set_busy(True)
        self.status.setText("Загружаем вкладки Google-таблицы…")
        self.worker.start()
        self.changed.emit()

    def tabs_loaded(self, names):
        previous = self.worksheet.currentText()
        self.worksheet.blockSignals(True)
        self.worksheet.clear()
        self.worksheet.addItems(names)
        if previous in names:
            self.worksheet.setCurrentText(previous)
        elif names:
            self.worksheet.setCurrentIndex(0)
        self.worksheet.blockSignals(False)
        self.invalidate()

    def tabs_result(self, success, message):
        self.status.setText("Выберите вкладку и нажмите «Проверить канал и таблицу»." if success else message)

    def verify(self):
        if self.busy():
            return
        try:
            channel = self.validated_value()
            candidate = copy.deepcopy(self.settings)
            candidate["channels"] = [channel]
            config = runtime_config(candidate, self.root)
        except Exception as exc:
            self.status.setText(friendly_error(exc))
            return
        self.checked = False
        self.worker = ChannelCheckWorker(config, config["channels"][0])
        self.worker.result.connect(self.result)
        self.worker.finished.connect(self.finished)
        self.set_busy(True)
        self.status.setText("Проверяем чтение канала и доступ к выбранной вкладке…")
        self.worker.start()
        self.changed.emit()

    def result(self, success, message):
        self.checked = success
        if self.save_draft:
            self.settings.update(channel_verified=success, google_verified=success)
            if success:
                self.settings["setup_checked_channel"] = self.value()
        self.status.setText(message)
        self.changed.emit()

    def set_busy(self, busy):
        self.inputs.setEnabled(not busy)
        self.columns.setEnabled(not busy)
        self.check_button.setEnabled(not busy)

    def finished(self):
        self.set_busy(False)
        self.changed.emit()

    def cancel(self):
        pass


class SetupWizard(QDialog):
    def __init__(self, root, settings):
        super().__init__()
        self.root, self.settings = root, settings
        self.closing = False
        self.setWindowTitle("AnalyticsBot — первоначальная настройка")
        self.resize(780, 650)
        self.setMinimumSize(700, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        self.heading = text("Подключим ваш аккаунт")
        self.heading.setObjectName("title")
        layout.addWidget(self.heading)
        self.steps = text("")
        layout.addWidget(self.steps)
        self.progress = QProgressBar()
        self.progress.setRange(0, 3)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        layout.addWidget(self.progress)
        self.stack = QStackedWidget()
        self.build_panels()
        layout.addWidget(self.stack, 1)
        self.hint = text("Прогресс сохраняется автоматически. Вы сможете продолжить позже.", True)
        layout.addWidget(self.hint)
        row = QHBoxLayout()
        self.back_button = action("Назад", self.back)
        self.next_button = action("Далее", self.next, True)
        row.addWidget(self.back_button)
        row.addStretch()
        row.addWidget(self.next_button)
        layout.addLayout(row)
        step = max(0, min(2, int(settings.get("setup_step", 0))))
        if not settings.get("telegram_verified"):
            step = 0
        elif not self.google.ready():
            step = min(step, 1)
        self.stack.setCurrentIndex(step)
        if step == 0:
            self.resize(780, 520)
        else:
            self.setMinimumHeight(580)
        self.changed()

    def build_panels(self):
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self.telegram = TelegramPanel(self.root, self.settings)
        self.google = GooglePanel(self.root, self.settings)
        self.channel = ChannelPanel(self.root, self.settings, save_draft=True)
        self.panels = [self.telegram, self.google, self.channel]
        for panel in self.panels:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(panel)
            self.stack.addWidget(scroll)
            panel.changed.connect(self.changed)

    def busy(self):
        return any(panel.busy() for panel in self.panels)

    def changed(self):
        self.settings["setup_step"] = self.stack.currentIndex()
        self.settings["setup_channel"] = self.channel.value()
        persist(self.root, self.settings)
        step = self.stack.currentIndex()
        self.heading.setText(("Подключим ваш аккаунт", "Подключим вашу таблицу", "Соединим Telegram и вашу таблицу")[step])
        self.steps.setText(("1 / 3  ·  Подключение Telegram", "2 / 3  ·  Подключение Google Sheets", "3 / 3  ·  Первый канал")[step])
        self.progress.setValue(step + 1)
        self.back_button.setEnabled(step > 0 and not self.busy())
        ready = [bool(self.settings.get("telegram_verified")), self.google.ready(), bool(self.channel.checked and self.settings.get("channel_verified") and self.settings.get("telegram_verified") and self.settings.get("google_verified"))][step]
        self.next_button.setEnabled(ready and not self.busy() and not self.closing)
        self.next_button.setText("Завершить" if step == 2 else "Далее")

    def back(self):
        if not self.busy():
            current = self.stack.currentIndex()
            self.stack.setCurrentIndex(max(0, current - 1))
            if current == 1:
                self.setMinimumHeight(500)
                if self.height() == 650:
                    self.resize(self.width(), 520)
            self.changed()

    def next(self):
        if not self.next_button.isEnabled():
            return
        step = self.stack.currentIndex()
        if step < 2:
            self.stack.setCurrentIndex(step + 1)
            if step == 0:
                self.setMinimumHeight(580)
                if self.height() <= 580:
                    self.resize(self.width(), 650)
            self.changed()
            return
        try:
            if not (self.settings.get("telegram_verified") and self.settings.get("google_verified") and self.settings.get("channel_verified") and self.channel.checked):
                raise ValueError("Завершите проверку Telegram, канала и таблицы.")
            candidate = copy.deepcopy(self.settings)
            candidate["channels"] = [self.channel.validated_value(), *self.settings["channels"][1:]]
            runtime_config(candidate, self.root)
            self.settings.update(channels=candidate["channels"], setup_complete=True, start_service=True)
            persist(self.root, self.settings)
            self.accept()
        except Exception as exc:
            self.hint.setText(friendly_error(exc))

    def reject(self):
        self.closing = True
        for panel in self.panels:
            panel.cancel()
        if self.busy():
            self.hint.setText("Завершаем подключение и сохраняем прогресс…")
            self.next_button.setEnabled(False)
            QTimer.singleShot(150, self.reject)
            return
        self.changed()
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()


class ConnectionDialog(QDialog):
    def __init__(self, root, settings, kind):
        super().__init__()
        self.root, self.settings, self.kind = root, settings, kind
        self.closing = False
        self.setWindowTitle("Подключение Telegram" if kind == "telegram" else "Ключ Google Sheets")
        self.resize(740, 610)
        self.draft = settings if kind == "telegram" else copy.deepcopy(settings)
        self.panel = TelegramPanel(root, self.draft) if kind == "telegram" else GooglePanel(root, self.draft, "google-key-pending.json", allow_check=True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.panel)
        layout.addWidget(scroll)
        self.save_button = action("Готово" if kind == "telegram" else "Сохранить ключ", self.commit, True)
        layout.addWidget(self.save_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.panel.changed.connect(self.changed)
        self.changed()

    def changed(self):
        ready = self.draft.get("telegram_verified") if self.kind == "telegram" else self.draft.get("google_verified") and self.panel.ready()
        self.save_button.setEnabled(bool(ready) and not self.panel.busy() and not self.closing)
        if self.kind == "telegram":
            persist(self.root, self.settings)

    def commit(self):
        if not self.save_button.isEnabled():
            return
        try:
            if self.kind == "google":
                pending = self.root / self.draft["credentials"]
                destination = self.root / "google-service-account.json"
                if pending != destination:
                    shutil.copy2(pending, destination.with_suffix(".tmp"))
                    destination.with_suffix(".tmp").replace(destination)
                self.settings.update(credentials=destination.name, google_email=self.draft["google_email"], google_verified=True)
                persist(self.root, self.settings)
        except Exception as exc:
            self.panel.status.setText(friendly_error(exc))
            return
        self.accept()

    def reject(self):
        self.closing = True
        self.panel.cancel()
        if self.panel.busy():
            self.save_button.setEnabled(False)
            QTimer.singleShot(150, self.reject)
            return
        if self.kind == "telegram" and not self.settings.get("telegram_verified"):
            self.settings["setup_complete"] = False
            persist(self.root, self.settings)
        super().reject()

    def done(self, result):
        pending = self.root / "google-key-pending.json"
        if self.kind == "google" and pending.is_file() and not self.panel.busy():
            pending.unlink()
        super().done(result)

    def closeEvent(self, event):
        event.ignore()
        self.reject()
