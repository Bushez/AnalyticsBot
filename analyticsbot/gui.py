import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from google.oauth2.service_account import Credentials

from .gui_config import DEFAULT_COLUMNS, channel_source, data_dir, import_cli, load_settings, runtime_config, save_settings, spreadsheet_id
from .gui_workers import GoogleWorker, ServiceWorker, TelegramWorker, friendly_error
from .sheets import HEADERS


STYLE = """
QWidget { background: #101011; color: #f7f8f8; font-family: 'Segoe UI'; font-size: 10pt; }
QLabel { background: transparent; }
QMainWindow, QDialog { background: #101011; }
QFrame#sidebar { background: #0f1011; border-right: 1px solid #23252a; }
QFrame#toolbar { background: #141516; border: 1px solid #23252a; border-radius: 6px; }
QGroupBox { background: transparent; border: 0; border-top: 1px solid #23252a; margin-top: 13px; padding: 12px 0 0; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 0; padding: 0 8px 0 0; color: #d0d6e0; }
QLabel#title { font-size: 15pt; font-weight: 600; }
QLabel#section { color: #d0d6e0; font-weight: 600; }
QLabel#subtitle { color: #8a8f98; }
QLabel#empty { color: #8a8f98; }
QLabel#metric { font-size: 10pt; font-weight: 600; }
QLabel#brand { font-size: 11pt; font-weight: 600; }
QPushButton { background: #18191a; color: #f7f8f8; border: 1px solid #34343a; border-radius: 5px; padding: 6px 12px; }
QPushButton:hover { background: #222326; border-color: #44454c; }
QPushButton:pressed { background: #101011; }
QPushButton:disabled { color: #62666d; background: #141516; border-color: #23252a; }
QPushButton#primary { background: #5e6ad2; border-color: #5e6ad2; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #828fff; border-color: #828fff; }
QPushButton#primary:disabled { background: #242534; color: #76798a; border-color: #242534; }
QPushButton#nav { background: transparent; color: #8a8f98; border: none; text-align: left; padding: 7px 10px; }
QPushButton#nav:hover { background: #18191a; color: #f7f8f8; }
QPushButton#nav:checked { background: #23252a; color: #f7f8f8; font-weight: 600; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTableWidget, QListWidget { background: #141516; border: 1px solid #34343a; border-radius: 5px; padding: 5px 7px; selection-background-color: #404861; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #5e69d1; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #62666d; border-color: #23252a; }
QHeaderView::section { background: #18191a; color: #8a8f98; border: none; border-bottom: 1px solid #34343a; padding: 6px 8px; font-weight: 600; }
QTableWidget { gridline-color: #23252a; }
QTableWidget::item { padding: 4px; }
QCheckBox { spacing: 8px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: #101011; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #3e3e44; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: #18191a; border: 1px solid #34343a; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 4px; }
QMenu::item:selected { background: #34343a; }
"""


def icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#5e6ad2"))
    painter.drawRoundedRect(3, 3, 58, 58, 15, 15)
    painter.setPen(QPen(QColor("#ffffff"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(16, 45, 16, 34)
    painter.drawLine(29, 45, 29, 24)
    painter.drawLine(42, 45, 42, 16)
    painter.end()
    return QIcon(pixmap)


def label(text, name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


def button(text, action, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(action)
    return widget


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().setVisible(False)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return widget


def windows_autostart(enabled=None):
    if os.name != "nt":
        return False
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if enabled is None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.QueryValueEx(key, "AnalyticsBot")
                return True
        except FileNotFoundError:
            return False
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        if enabled:
            if getattr(sys, "frozen", False):
                command = f'"{sys.executable}" --background'
            else:
                pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
                entry = str(Path(__file__).resolve().parent.parent / "gui_entry.py")
                command = f'"{pythonw}" "{entry}" --background'
            winreg.SetValueEx(key, "AnalyticsBot", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "AnalyticsBot")
            except FileNotFoundError:
                pass
    return bool(enabled)


class ChannelDialog(QDialog):
    def __init__(self, parent, channel=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки канала")
        self.resize(580, 620)
        item = channel or {"source": "", "spreadsheet": "", "worksheet": "", "columns": DEFAULT_COLUMNS.copy()}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label("Канал и таблица", "title"))
        layout.addWidget(label("Сервис должен иметь доступ к каналу и к выбранному листу.", "subtitle"))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.source = QLineEdit(str(item["source"]))
        self.source.setPlaceholderText("https://t.me/channel, приглашение или @channel")
        self.spreadsheet = QLineEdit(item["spreadsheet"])
        self.spreadsheet.setPlaceholderText("https://docs.google.com/spreadsheets/d/...")
        self.worksheet = QLineEdit(item["worksheet"])
        self.worksheet.setPlaceholderText("Название вкладки внутри таблицы")
        form.addRow("Telegram-канал", self.source)
        form.addRow("Google-таблица", self.spreadsheet)
        form.addRow("Вкладка", self.worksheet)
        layout.addLayout(form)
        group = QGroupBox("Сопоставление столбцов")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 4, 0, 0)
        group_layout.setSpacing(8)
        group_layout.addWidget(label("Укажите буквы существующих столбцов. Служебные поля будут скрыты.", "subtitle"))
        self.columns = QTableWidget(len(DEFAULT_COLUMNS), 2)
        self.columns.setHorizontalHeaderLabels(["Поле", "Столбец"])
        self.columns.verticalHeader().setVisible(False)
        self.columns.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.columns.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for row, field in enumerate(DEFAULT_COLUMNS):
            title = QTableWidgetItem(HEADERS[field])
            title.setFlags(title.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.columns.setItem(row, 0, title)
            self.columns.setItem(row, 1, QTableWidgetItem(item["columns"].get(field, DEFAULT_COLUMNS[field])))
        group_layout.addWidget(self.columns)
        layout.addWidget(group, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return {"source": self.source.text().strip(), "spreadsheet": self.spreadsheet.text().strip(),
                "worksheet": self.worksheet.text().strip(),
                "columns": {field: self.columns.item(row, 1).text().strip().upper() if self.columns.item(row, 1) else ""
                            for row, field in enumerate(DEFAULT_COLUMNS)}}

    def validate_and_accept(self):
        try:
            item = self.value()
            channel_source(item["source"])
            spreadsheet_id(item["spreadsheet"])
            if not item["worksheet"]:
                raise ValueError("Укажите название вкладки Google-таблицы")
            import re
            columns = list(item["columns"].values())
            if any(not re.fullmatch(r"[A-Z]{1,3}", col) for col in columns) or len(set(columns)) != len(columns):
                raise ValueError("Укажите разные буквы столбцов, например A или AA")
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, root, settings, background=False):
        super().__init__()
        self.root = root
        self.settings = settings
        self.worker = None
        self.auth_worker = None
        self.google_worker = None
        self.qr_dialog = None
        self.exiting = False
        self.backfill_active = False
        self.service_failed = False
        self.setWindowTitle("AnalyticsBot")
        self.setWindowIcon(icon())
        self.resize(920, 640)
        self.setMinimumSize(820, 600)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(168)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 14)
        side.setSpacing(4)
        side.addWidget(label("AnalyticsBot", "brand"))
        side.addSpacing(16)
        self.stack = QStackedWidget()
        self.nav = []
        for index, title in enumerate(("Главная", "Каналы", "Настройки")):
            item = QPushButton(title)
            item.setObjectName("nav")
            item.setCheckable(True)
            item.clicked.connect(lambda checked=False, i=index: self.navigate(i))
            side.addWidget(item)
            self.nav.append(item)
        side.addStretch()
        side.addWidget(label("Сбор через 24 часа", "subtitle"))
        outer.addWidget(sidebar)
        outer.addWidget(self.stack, 1)
        self.stack.addWidget(self.dashboard())
        self.channels_page = self.channels_view()
        self.stack.addWidget(self.channels_page)
        self.settings_page = self.settings_view()
        self.stack.addWidget(self.settings_page)
        self.navigate(0)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("AnalyticsBot")
        menu = QMenu(self)
        show_action = QAction("Открыть", self)
        show_action.triggered.connect(self.show_window)
        quit_action = QAction("Завершить работу", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_dashboard)
        self.timer.start(10000)
        self.refresh_channels()
        self.refresh_dashboard()
        if settings.get("start_service"):
            QTimer.singleShot(0, self.start_service)
        if background and self.tray.isSystemTrayAvailable():
            QTimer.singleShot(0, self.hide)

    def page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(label(title, "title"))
        layout.addWidget(label(subtitle, "subtitle"))
        return page, layout

    def dashboard(self):
        page, layout = self.page("Главная", "Состояние сервиса и очередь сбора")
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(12, 8, 12, 8)
        bar.setSpacing(12)
        bar.addWidget(label("Сервис", "subtitle"))
        self.status = label("Остановлен", "metric")
        bar.addWidget(self.status)
        self.channel_count = label("Каналов: 0", "subtitle")
        bar.addWidget(self.channel_count)
        bar.addStretch()
        self.start_button = button("Запустить", self.start_service, True)
        self.stop_button = button("Остановить", self.stop_service)
        self.stop_button.setEnabled(False)
        bar.addWidget(self.start_button)
        bar.addWidget(self.stop_button)
        layout.addWidget(toolbar)
        layout.addSpacing(6)
        layout.addWidget(label("Ближайшие сборы", "section"))
        self.upcoming = table(["Пост", "Плановое время"])
        self.upcoming.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.upcoming.setMaximumHeight(180)
        layout.addWidget(self.upcoming)
        self.upcoming_empty = label("Нет запланированных сборов", "empty")
        layout.addWidget(self.upcoming_empty)
        layout.addSpacing(6)
        layout.addWidget(label("События", "section"))
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(300)
        self.events.setPlaceholderText("Здесь будут появляться события сервиса")
        self.events.setFixedHeight(88)
        layout.addWidget(self.events)
        layout.addStretch()
        return page

    def channels_view(self):
        page, layout = self.page("Каналы", "Настройте, куда записывать статистику каждого канала")
        self.channels_table = table(["Канал", "Google-таблица", "Вкладка"])
        self.channels_table.doubleClicked.connect(self.edit_channel)
        self.channels_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.channels_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(button("+  Добавить", self.add_channel, True))
        self.edit_channel_button = button("Изменить", self.edit_channel)
        self.delete_channel_button = button("Удалить", self.delete_channel)
        self.backfill_button = button("Дозагрузить посты", self.start_backfill)
        actions.addWidget(self.edit_channel_button)
        actions.addWidget(self.delete_channel_button)
        actions.addWidget(self.backfill_button)
        actions.addStretch()
        self.channels_table.itemSelectionChanged.connect(self.update_channel_actions)
        layout.addLayout(actions)
        layout.addWidget(label("Остановите сервис перед изменением каналов.", "subtitle"))
        self.channels_content = QStackedWidget()
        self.channels_content.addWidget(self.channels_table)
        empty = label("Каналы ещё не добавлены.\nИспользуйте кнопку «Добавить», чтобы настроить первый канал.", "empty")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.channels_content.addWidget(empty)
        layout.addWidget(self.channels_content, 1)
        layout.addStretch()
        return page

    def settings_view(self):
        page, layout = self.page("Настройки", "Авторизация и параметры работы")
        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(10)
        telegram_group = QGroupBox("Telegram")
        form = QFormLayout(telegram_group)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(7)
        self.api_id = QLineEdit(str(self.settings.get("api_id", "")))
        self.api_id.setPlaceholderText("Получите на my.telegram.org → API development tools")
        self.api_hash = QLineEdit(self.settings.get("api_hash", ""))
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone = QLineEdit(self.settings.get("phone", ""))
        self.phone.setPlaceholderText("+7...")
        self.code = QLineEdit()
        self.code.setPlaceholderText("Код подтверждения")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Пароль двухэтапной защиты")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API ID", self.api_id)
        form.addRow("API hash", self.api_hash)
        form.addRow("Телефон", self.phone)
        auth_actions = QHBoxLayout()
        self.auth_button = button("Получить код", self.start_auth, True)
        self.qr_button = button("Войти по QR", self.start_qr)
        self.check_telegram_button = button("Проверить подключение", self.check_telegram)
        self.logout_button = button("Сменить аккаунт", self.logout_telegram)
        auth_actions.addWidget(self.auth_button)
        auth_actions.addWidget(self.qr_button)
        auth_actions.addStretch()
        form.addRow("", auth_actions)
        self.cancel_auth_button = button("Отменить авторизацию", self.cancel_auth)
        self.cancel_auth_button.setEnabled(False)
        cancel_row = QHBoxLayout()
        cancel_row.addWidget(self.check_telegram_button)
        cancel_row.addWidget(self.logout_button)
        cancel_row.addWidget(self.cancel_auth_button)
        cancel_row.addStretch()
        form.addRow("", cancel_row)
        self.code_button = button("Подтвердить код", self.submit_code)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code)
        code_row.addWidget(self.code_button)
        form.addRow("Код", code_row)
        self.password_button = button("Подтвердить пароль", self.submit_password)
        password_row = QHBoxLayout()
        password_row.addWidget(self.password)
        password_row.addWidget(self.password_button)
        form.addRow("2FA", password_row)
        self.code_button.setEnabled(False)
        self.password_button.setEnabled(False)
        self.auth_status = label("Код может прийти в служебный чат Telegram.", "subtitle")
        form.addRow("", self.auth_status)
        body.addWidget(telegram_group)
        google_group = QGroupBox("Google Sheets")
        google_form = QFormLayout(google_group)
        google_form.setHorizontalSpacing(14)
        google_form.setVerticalSpacing(7)
        self.credentials_status = label(self.settings.get("credentials") or "JSON-файл не выбран", "subtitle")
        google_form.addRow("Ключ", self.credentials_status)
        self.google_channels = QComboBox()
        google_form.addRow("Канал", self.google_channels)
        google_actions = QHBoxLayout()
        google_actions.addWidget(button("Выбрать JSON", self.choose_credentials))
        self.google_button = button("Проверить доступ", self.check_google)
        google_actions.addWidget(self.google_button)
        google_actions.addStretch()
        google_form.addRow("", google_actions)
        google_form.addRow("", label("Откройте таблицу для email сервисного аккаунта с правом редактора.", "subtitle"))
        body.addWidget(google_group)
        options = QGroupBox("Сбор и запуск")
        options_form = QFormLayout(options)
        options_form.setHorizontalSpacing(14)
        options_form.setVerticalSpacing(7)
        self.poll = self.spin(self.settings["poll_seconds"], 10, 86400)
        self.retry = self.spin(self.settings["retry_seconds"], 10, 86400)
        self.lookback = self.spin(self.settings["initial_lookback_hours"], 1, 8760)
        self.timezone = QLineEdit(self.settings.get("timezone", "Europe/Moscow"))
        self.start_service_box = QCheckBox("Запускать сбор при открытии приложения")
        self.start_service_box.setChecked(bool(self.settings.get("start_service")))
        self.autostart = QCheckBox("Открывать приложение вместе с Windows")
        self.autostart.setEnabled(os.name == "nt")
        self.autostart.setChecked(windows_autostart())
        options_form.addRow("Опрос, секунд", self.poll)
        options_form.addRow("Повтор, секунд", self.retry)
        options_form.addRow("Первый поиск, часов", self.lookback)
        options_form.addRow("Часовой пояс", self.timezone)
        options_form.addRow("", self.start_service_box)
        options_form.addRow("", self.autostart)
        body.addWidget(options)
        settings_actions = QHBoxLayout()
        settings_actions.addWidget(button("Сохранить настройки", self.save_options, True))
        settings_actions.addWidget(button("Импортировать данные сервиса", self.import_existing))
        settings_actions.addStretch()
        body.addLayout(settings_actions)
        body.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        return page

    def spin(self, value, minimum, maximum):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(int(value))
        return widget

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        for i, item in enumerate(self.nav):
            item.setChecked(i == index)

    def persist(self):
        save_settings(self.root / "settings.json", self.settings)

    def collect_options(self):
        self.settings.update(api_id=self.api_id.text().strip(), api_hash=self.api_hash.text().strip(),
                             phone=self.phone.text().strip(), poll_seconds=self.poll.value(), retry_seconds=self.retry.value(),
                             initial_lookback_hours=self.lookback.value(), timezone=self.timezone.text().strip(),
                             start_service=self.start_service_box.isChecked())

    def save_options(self):
        try:
            from zoneinfo import ZoneInfo
            self.collect_options()
            ZoneInfo(self.settings["timezone"])
            windows_autostart(self.autostart.isChecked())
            self.persist()
            self.record_event(logging.INFO, "Настройки сохранены")
        except Exception as exc:
            QMessageBox.warning(self, "Настройки", friendly_error(exc))

    def refresh_channels(self):
        channels = self.settings["channels"]
        self.channels_table.setRowCount(len(channels))
        self.channels_content.setCurrentIndex(0 if channels else 1)
        self.channels_content.setMaximumHeight(min(380, max(104, 44 + 31 * len(channels))))
        for row, item in enumerate(channels):
            for col, value in enumerate((str(item["source"]), item["spreadsheet"], item["worksheet"])):
                self.channels_table.setItem(row, col, QTableWidgetItem(value))
        self.channel_count.setText(f"Каналов: {len(channels)}")
        current = self.google_channels.currentIndex()
        self.google_channels.clear()
        self.google_channels.addItems([str(item["source"]) for item in channels])
        if channels:
            self.google_channels.setCurrentIndex(min(max(current, 0), len(channels) - 1))
        self.update_channel_actions()

    def update_channel_actions(self):
        selected = bool(self.channels_table.selectedItems())
        for item in (self.edit_channel_button, self.delete_channel_button, self.backfill_button):
            item.setEnabled(selected)

    def add_channel(self):
        dialog = ChannelDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings["channels"].append(dialog.value())
            self.persist()
            self.refresh_channels()

    def edit_channel(self):
        row = self.channels_table.currentRow()
        if row < 0:
            return
        dialog = ChannelDialog(self, self.settings["channels"][row])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings["channels"][row] = dialog.value()
            self.persist()
            self.refresh_channels()

    def delete_channel(self):
        row = self.channels_table.currentRow()
        if row < 0:
            return
        name = self.settings["channels"][row]["source"]
        if QMessageBox.question(self, "Удалить канал", f"Удалить {name} из настроек?") == QMessageBox.StandardButton.Yes:
            del self.settings["channels"][row]
            self.persist()
            self.refresh_channels()

    def choose_credentials(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ключ сервисного аккаунта", "", "JSON (*.json)")
        if not path:
            return
        try:
            credentials = Credentials.from_service_account_file(path)
            if not credentials.service_account_email:
                raise ValueError("В файле нет email сервисного аккаунта")
            destination = self.root / "google-service-account.json"
            if Path(path).resolve() != destination.resolve():
                shutil.copy2(path, destination)
            self.settings["credentials"] = destination.name
            self.credentials_status.setText(credentials.service_account_email)
            self.persist()
        except Exception as exc:
            QMessageBox.warning(self, "Ключ Google", "Не удалось прочитать JSON-ключ сервисного аккаунта. Проверьте файл.")

    def telegram_credentials(self):
        self.collect_options()
        try:
            api_id = int(self.settings["api_id"])
            if api_id < 1 or not self.settings["api_hash"]:
                raise ValueError
        except ValueError:
            raise ValueError("Укажите Telegram API ID и API hash") from None
        self.persist()
        return api_id, self.settings["api_hash"]

    def logout_telegram(self):
        if QMessageBox.question(self, "Telegram", "Завершить сессию этого приложения и войти в другой аккаунт?") != QMessageBox.StandardButton.Yes:
            return
        try:
            api_id, api_hash = self.telegram_credentials()
            self.launch_telegram(api_id, api_hash, None, logout=True)
        except Exception as exc:
            QMessageBox.warning(self, "Telegram", friendly_error(exc))

    def start_auth(self):
        if self.auth_worker and self.auth_worker.isRunning():
            return
        try:
            api_id, api_hash = self.telegram_credentials()
            if not self.settings["phone"].startswith("+"):
                raise ValueError("Укажите номер в международном формате, например +7...")
            self.launch_telegram(api_id, api_hash, self.settings["phone"])
        except Exception as exc:
            QMessageBox.warning(self, "Telegram", friendly_error(exc))

    def check_telegram(self):
        try:
            api_id, api_hash = self.telegram_credentials()
            self.launch_telegram(api_id, api_hash, None)
        except Exception as exc:
            QMessageBox.warning(self, "Telegram", friendly_error(exc))

    def start_qr(self):
        try:
            api_id, api_hash = self.telegram_credentials()
            self.launch_telegram(api_id, api_hash, None, qr_mode=True)
        except Exception as exc:
            QMessageBox.warning(self, "Telegram", friendly_error(exc))

    def launch_telegram(self, api_id, api_hash, phone, logout=False, qr_mode=False):
        if self.auth_worker and self.auth_worker.isRunning():
            return
        self.auth_worker = TelegramWorker(api_id, api_hash, str(self.root / "telegram"), str(self.root / "jobs.sqlite3"), phone, logout, qr_mode)
        self.auth_worker.stage.connect(self.auth_stage)
        self.auth_worker.qr_ready.connect(self.show_qr)
        self.auth_worker.result.connect(self.auth_result)
        self.auth_worker.finished.connect(self.auth_finished)
        self.auth_button.setEnabled(False)
        self.qr_button.setEnabled(False)
        self.check_telegram_button.setEnabled(False)
        self.logout_button.setEnabled(False)
        self.cancel_auth_button.setEnabled(True)
        self.auth_status.setText("Подключение к Telegram…")
        self.auth_worker.start()

    def auth_stage(self, stage, text):
        self.auth_status.setText(text)
        self.code_button.setEnabled(stage == "code")
        self.password_button.setEnabled(stage == "password")
        if stage == "password" and self.qr_dialog:
            self.qr_dialog.hide()
        if stage in ("code", "password"):
            (self.code if stage == "code" else self.password).setFocus()

    def show_qr(self, url):
        import qrcode

        code = qrcode.QRCode(border=4)
        code.add_data(url)
        matrix = code.get_matrix()
        scale = 7
        image = QImage(len(matrix) * scale, len(matrix) * scale, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("black"))
        for y, row in enumerate(matrix):
            for x, value in enumerate(row):
                if value:
                    painter.drawRect(x * scale, y * scale, scale, scale)
        painter.end()
        if self.qr_dialog is None:
            self.qr_dialog = QDialog(self)
            self.qr_dialog.setWindowTitle("Вход в Telegram")
            layout = QVBoxLayout(self.qr_dialog)
            layout.addWidget(label("В Telegram откройте Настройки → Устройства → Подключить устройство."))
            self.qr_image = QLabel()
            self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.qr_image)
            layout.addWidget(label("QR-код обновится автоматически. Не показывайте его другим.", "subtitle"))
            layout.addWidget(button("Отменить", self.qr_dialog.reject))
            self.qr_dialog.rejected.connect(lambda: self.auth_worker.cancel() if self.auth_worker and self.auth_worker.isRunning() else None)
        self.qr_image.setPixmap(QPixmap.fromImage(image))
        self.qr_dialog.adjustSize()
        self.qr_dialog.show()
        self.qr_dialog.raise_()

    def submit_code(self):
        if self.auth_worker and self.code.text().strip():
            self.code_button.setEnabled(False)
            self.auth_worker.answer(self.code.text().strip())
            self.code.clear()

    def cancel_auth(self):
        if self.auth_worker and self.auth_worker.isRunning():
            self.auth_worker.cancel()
            self.auth_status.setText("Отмена авторизации…")

    def submit_password(self):
        if self.auth_worker and self.password.text():
            self.password_button.setEnabled(False)
            self.auth_worker.answer(self.password.text())
            self.password.clear()

    def auth_result(self, success, message):
        self.auth_status.setText(message)
        self.record_event(logging.INFO if success else logging.ERROR, message)

    def auth_finished(self):
        self.code_button.setEnabled(False)
        self.password_button.setEnabled(False)
        self.auth_button.setEnabled(True)
        self.qr_button.setEnabled(True)
        self.check_telegram_button.setEnabled(True)
        self.logout_button.setEnabled(True)
        self.cancel_auth_button.setEnabled(False)
        if self.qr_dialog:
            self.qr_dialog.hide()

    def import_existing(self):
        if self.auth_worker and self.auth_worker.isRunning():
            QMessageBox.warning(self, "Импорт", "Сначала завершите авторизацию Telegram.")
            return
        directory = QFileDialog.getExistingDirectory(self, "Выберите папку существующего AnalyticsBot")
        if not directory:
            return
        try:
            self.settings = import_cli(Path(directory), self.root)
            self.api_id.setText(str(self.settings["api_id"]))
            self.api_hash.setText(self.settings["api_hash"])
            self.phone.setText(self.settings["phone"])
            self.credentials_status.setText(self.settings["credentials"])
            self.poll.setValue(self.settings["poll_seconds"])
            self.retry.setValue(self.settings["retry_seconds"])
            self.lookback.setValue(self.settings["initial_lookback_hours"])
            self.timezone.setText(self.settings["timezone"])
            self.refresh_channels()
            self.refresh_dashboard()
            QMessageBox.information(self, "Импорт", "Настройки, очередь и доступные файлы авторизации импортированы.")
        except Exception as exc:
            QMessageBox.warning(self, "Импорт", friendly_error(exc))

    def check_google(self):
        if self.google_worker and self.google_worker.isRunning():
            return
        try:
            self.collect_options()
            if not self.settings["channels"]:
                raise ValueError("Добавьте канал с таблицей для проверки")
            credential = self.root / self.settings.get("credentials", "")
            if not credential.is_file():
                raise ValueError("Выберите JSON-ключ сервисного аккаунта")
            first = self.settings["channels"][max(0, self.google_channels.currentIndex())]
            channel = {"sheet": {"spreadsheet_id": spreadsheet_id(first["spreadsheet"]), "worksheet": first["worksheet"]}}
            self.google_worker = GoogleWorker(str(credential), channel)
            self.google_worker.result.connect(self.google_result)
            self.google_worker.finished.connect(lambda: self.google_button.setEnabled(True))
            self.google_button.setEnabled(False)
            self.google_worker.start()
        except Exception as exc:
            QMessageBox.warning(self, "Google Sheets", friendly_error(exc))

    def google_result(self, success, message):
        self.record_event(logging.INFO if success else logging.ERROR, message)
        if not self.exiting:
            (QMessageBox.information if success else QMessageBox.warning)(self, "Google Sheets", message)

    def start_service(self):
        if self.worker and self.worker.isRunning():
            return
        if self.auth_worker and self.auth_worker.isRunning():
            QMessageBox.warning(self, "Telegram", "Завершите авторизацию перед запуском сервиса.")
            return
        try:
            self.collect_options()
            config = runtime_config(self.settings, self.root)
            self.persist()
        except Exception as exc:
            self.record_event(logging.ERROR, friendly_error(exc))
            if self.isVisible():
                QMessageBox.warning(self, "Не удалось запустить", friendly_error(exc))
            return
        self.worker = ServiceWorker(config)
        self.backfill_active = False
        self.launch_worker()

    def start_backfill(self):
        if self.worker and self.worker.isRunning():
            return
        row = self.channels_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Дозагрузка", "Выберите канал в списке.")
            return
        if self.auth_worker and self.auth_worker.isRunning():
            QMessageBox.warning(self, "Telegram", "Сначала завершите авторизацию.")
            return
        count, accepted = QInputDialog.getInt(self, "Дозагрузка постов", "Сколько последних постов старше 24 часов загрузить?", 10, 1, 10000)
        if not accepted:
            return
        try:
            self.collect_options()
            config = runtime_config(self.settings, self.root)
            self.persist()
        except Exception as exc:
            QMessageBox.warning(self, "Дозагрузка", friendly_error(exc))
            return
        self.worker = ServiceWorker(config, str(config["channels"][row]["source"]), count)
        self.backfill_active = True
        self.launch_worker()

    def launch_worker(self):
        self.service_failed = False
        self.worker.event.connect(self.record_event)
        self.worker.ready.connect(self.service_ready)
        self.worker.error.connect(self.service_error)
        self.worker.finished.connect(self.service_finished)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.channels_page.setEnabled(False)
        self.settings_page.setEnabled(False)
        self.status.setText("Дозагрузка…" if self.backfill_active else "Запускается…")
        self.record_event(logging.INFO, "Дозагрузка постов…" if self.backfill_active else "Подключение к Telegram и таблицам…")
        self.worker.start()

    def service_ready(self):
        self.status.setText("Работает")
        self.record_event(logging.INFO, "Сервис запущен")

    def service_error(self, message):
        self.service_failed = True
        self.record_event(logging.ERROR, message)
        if self.isVisible() and not self.exiting:
            QMessageBox.warning(self, "Сервис", message)
        elif self.tray.isVisible():
            self.tray.showMessage("AnalyticsBot", message, QSystemTrayIcon.MessageIcon.Warning)

    def stop_service(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("Останавливается…")
            self.stop_button.setEnabled(False)
            self.worker.stop()

    def service_finished(self):
        self.status.setText("Остановлен")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.channels_page.setEnabled(True)
        self.settings_page.setEnabled(True)
        if self.backfill_active:
            message = "Дозагрузка завершилась с ошибкой" if self.service_failed else "Дозагрузка остановлена" if self.worker.stop_requested else "Дозагрузка завершена"
            self.record_event(logging.ERROR if self.service_failed else logging.INFO, message)
        else:
            self.record_event(logging.INFO, "Сервис остановлен")
        self.refresh_dashboard()

    def record_event(self, level, message):
        kind = "Ошибка" if level >= logging.ERROR else "Событие"
        self.events.setFixedHeight(170)
        self.events.appendPlainText(f"{datetime.now():%H:%M:%S}  {kind}  ·  {message}")

    def refresh_dashboard(self):
        path = self.root / "jobs.sqlite3"
        if not path.is_file():
            self.upcoming.setRowCount(0)
            self.upcoming.hide()
            self.upcoming_empty.show()
            return
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.2)
            try:
                rows = connection.execute("SELECT post_key, MAX(due_at, next_attempt, published_at + 86400) FROM jobs WHERE status = 'pending' ORDER BY MAX(due_at, next_attempt, published_at + 86400) LIMIT 10").fetchall()
            finally:
                connection.close()
            self.upcoming.setRowCount(len(rows))
            self.upcoming.setMaximumHeight(min(180, max(72, 44 + 31 * len(rows))))
            self.upcoming.setVisible(bool(rows))
            self.upcoming_empty.setVisible(not rows)
            for row, (key, when) in enumerate(rows):
                self.upcoming.setItem(row, 0, QTableWidgetItem(str(key)))
                self.upcoming.setItem(row, 1, QTableWidgetItem(datetime.fromtimestamp(when).strftime("%d.%m.%Y %H:%M")))
        except (sqlite3.Error, OSError, ValueError):
            return

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def closeEvent(self, event):
        if not self.exiting and self.tray.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            self.tray.showMessage("AnalyticsBot", "Приложение продолжает работать в трее.", QSystemTrayIcon.MessageIcon.Information, 3000)
            return
        self.quit_app()
        event.ignore()

    def quit_app(self):
        self.exiting = True
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        if self.auth_worker and self.auth_worker.isRunning():
            self.auth_worker.cancel()
        if any(worker and worker.isRunning() for worker in (self.worker, self.auth_worker, self.google_worker)):
            self.status.setText("Завершение работы…")
            QTimer.singleShot(300, self.quit_app)
            return
        self.tray.hide()
        QApplication.instance().quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AnalyticsBot")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    if "--self-test" in sys.argv:
        import tempfile
        try:
            from google.auth.credentials import AnonymousCredentials
            from googleapiclient.discovery import build
            from zoneinfo import ZoneInfo
            build("sheets", "v4", credentials=AnonymousCredentials(), static_discovery=True)
            ZoneInfo("Europe/Moscow")
            with tempfile.TemporaryDirectory() as directory:
                window = MainWindow(Path(directory), load_settings(Path(directory) / "settings.json"))
                QTimer.singleShot(200, app.quit)
                result = app.exec()
                window.tray.hide()
                window.timer.stop()
                return result
        except Exception:
            return 1
    try:
        root = data_dir()
        settings = load_settings(root / "settings.json")
        window = MainWindow(root, settings, "--background" in sys.argv)
    except Exception as exc:
        QMessageBox.critical(None, "AnalyticsBot", f"Не удалось открыть приложение: {friendly_error(exc)}")
        return 1
    if "--background" not in sys.argv or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    return app.exec()
