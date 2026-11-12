import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFormLayout,
                               QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from .gui_config import DEFAULT_COLUMNS, data_dir, load_settings, runtime_config, save_settings
from .gui_workers import ServiceWorker, friendly_error


STYLE = """
QWidget { background: #101011; color: #f7f8f8; font-family: 'Segoe UI', '.AppleSystemUIFont'; font-size: 10pt; }
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
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #62666d; border-radius: 3px; background: #141516; }
QCheckBox::indicator:checked { background: #5e6ad2; border-color: #828fff; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: #101011; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #3e3e44; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: #18191a; border: 1px solid #34343a; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 4px; }
QMenu::item:selected { background: #34343a; }
QToolButton { color: #d0d6e0; background: transparent; border: none; padding: 5px 0; text-align: left; }
QToolButton:hover { color: #f7f8f8; }
QProgressBar { background: #23252a; border: none; border-radius: 2px; }
QProgressBar::chunk { background: #5e6ad2; border-radius: 2px; }
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
        from .gui_setup import ChannelPanel
        self.setWindowTitle("Настройки канала")
        self.resize(720, 530)
        self.panel = ChannelPanel(parent.root, parent.settings, channel or {"source": "", "spreadsheet": "", "worksheet": "", "columns": DEFAULT_COLUMNS.copy()})
        self.closing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(label("Канал и таблица", "title"))
        layout.addWidget(self.panel)
        self.save_button = button("Сохранить", self.validate_and_accept, True)
        layout.addWidget(self.save_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.panel.changed.connect(lambda: self.save_button.setEnabled(self.panel.checked and not self.panel.busy() and not self.closing))
        self.save_button.setEnabled(False)

    def value(self):
        return self.panel.value()

    def validate_and_accept(self):
        if self.panel.checked and not self.panel.busy():
            self.panel.validated_value()
            self.accept()

    def reject(self):
        self.closing = True
        self.panel.cancel()
        if self.panel.busy():
            self.save_button.setEnabled(False)
            QTimer.singleShot(150, self.reject)
            return
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()


class MainWindow(QMainWindow):
    def __init__(self, root, settings, background=False):
        super().__init__()
        self.root = root
        self.settings = settings
        self.worker = None
        self.active_dialog = None
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
        if settings.get("setup_complete") or settings.get("start_service"):
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
        page, layout = self.page("Настройки", "Подключения и запуск приложения")
        from PySide6.QtWidgets import QScrollArea, QToolButton
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(12)
        connections = QFormLayout()
        self.telegram_summary = label("", "subtitle")
        telegram_row = QHBoxLayout()
        telegram_row.addWidget(self.telegram_summary, 1)
        telegram_row.addWidget(button("Сменить аккаунт", self.change_telegram))
        connections.addRow("Telegram", telegram_row)
        self.credentials_status = label("", "subtitle")
        google_row = QHBoxLayout()
        google_row.addWidget(self.credentials_status, 1)
        google_row.addWidget(button("Изменить ключ", self.change_google))
        connections.addRow("Google Sheets", google_row)
        body.addLayout(connections)
        self.refresh_connection_labels()
        options = QFormLayout()
        self.timezone = QLineEdit(self.settings.get("timezone", "Europe/Moscow"))
        self.autostart = QCheckBox("Открывать приложение вместе с Windows" if os.name == "nt" else "Открывать приложение при входе в систему")
        self.autostart.setEnabled(os.name == "nt")
        if sys.platform == "darwin":
            self.autostart.setToolTip("Добавьте AnalyticsBot в Системные настройки → Основные → Объекты входа.")
        self.autostart.setChecked(windows_autostart())
        options.addRow("Часовой пояс", self.timezone)
        options.addRow("", self.autostart)
        body.addLayout(options)
        body.addWidget(label("Мониторинг запускается автоматически при открытии настроенного приложения.", "subtitle"))
        advanced_toggle = QToolButton()
        advanced_toggle.setText("Дополнительные настройки")
        advanced_toggle.setCheckable(True)
        advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        body.addWidget(advanced_toggle)
        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_form = QFormLayout()
        self.poll = self.spin(self.settings["poll_seconds"], 10, 86400)
        self.retry = self.spin(self.settings["retry_seconds"], 10, 86400)
        self.lookback = self.spin(self.settings["initial_lookback_hours"], 1, 8760)
        advanced_form.addRow("Проверка новых постов, сек.", self.poll)
        advanced_form.addRow("Повтор после ошибки, сек.", self.retry)
        advanced_form.addRow("История при первом запуске, ч.", self.lookback)
        advanced_layout.addLayout(advanced_form)
        advanced_layout.addWidget(label("Опрос определяет частоту поиска новых публикаций. Повтор — паузу после ошибки. История используется только при первом подключении канала. Статистика всегда собирается не раньше 24 часов после публикации.", "subtitle"))
        advanced.hide()
        advanced_toggle.toggled.connect(advanced.setVisible)
        advanced_toggle.toggled.connect(lambda opened: advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow))
        body.addWidget(advanced)
        body.addWidget(button("Сохранить настройки", self.save_options, True), alignment=Qt.AlignmentFlag.AlignLeft)
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
        self.settings.update(poll_seconds=self.poll.value(), retry_seconds=self.retry.value(),
                             initial_lookback_hours=self.lookback.value(), timezone=self.timezone.text().strip())

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
        self.update_channel_actions()

    def update_channel_actions(self):
        selected = bool(self.channels_table.selectedItems())
        for item in (self.edit_channel_button, self.delete_channel_button, self.backfill_button):
            item.setEnabled(selected)

    def add_channel(self):
        dialog = ChannelDialog(self)
        self.active_dialog = dialog
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings["channels"].append(dialog.value())
            self.persist()
            self.refresh_channels()
        self.active_dialog = None

    def edit_channel(self):
        row = self.channels_table.currentRow()
        if row < 0:
            return
        dialog = ChannelDialog(self, self.settings["channels"][row])
        self.active_dialog = dialog
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings["channels"][row] = dialog.value()
            self.persist()
            self.refresh_channels()
        self.active_dialog = None

    def delete_channel(self):
        row = self.channels_table.currentRow()
        if row < 0:
            return
        name = self.settings["channels"][row]["source"]
        if QMessageBox.question(self, "Удалить канал", f"Удалить {name} из настроек?") == QMessageBox.StandardButton.Yes:
            del self.settings["channels"][row]
            self.persist()
            self.refresh_channels()


    def refresh_connection_labels(self):
        account = (self.settings.get("telegram_account") or "Telegram") if self.settings.get("telegram_verified") else "Не подключён"
        self.telegram_summary.setText(account)
        self.credentials_status.setText(self.settings.get("google_email") or "Ключ не выбран")

    def change_telegram(self):
        from .gui_setup import ConnectionDialog
        dialog = ConnectionDialog(self.root, self.settings, "telegram")
        self.active_dialog = dialog
        dialog.exec()
        self.active_dialog = None
        self.refresh_connection_labels()

    def change_google(self):
        from .gui_setup import ConnectionDialog
        dialog = ConnectionDialog(self.root, self.settings, "google")
        self.active_dialog = dialog
        dialog.exec()
        self.active_dialog = None
        self.refresh_connection_labels()

    def start_service(self):
        if self.worker and self.worker.isRunning():
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
        if self.active_dialog is not None:
            dialog = self.active_dialog
            dialog.reject()
            if dialog.panel.busy():
                self.status.setText("Завершение подключения…")
                QTimer.singleShot(150, self.quit_app)
                return
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        if self.worker and self.worker.isRunning():
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
                from .gui_setup import SetupWizard
                wizard = SetupWizard(Path(directory), load_settings(Path(directory) / "settings.json"))
                assert not wizard.next_button.isEnabled()
                wizard.reject()
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
        completed_now = False
        if not settings.get("setup_complete"):
            from .gui_setup import SetupWizard
            wizard = SetupWizard(root, settings)
            if wizard.exec() != QDialog.DialogCode.Accepted:
                return 0
            completed_now = True
        background = "--background" in sys.argv and not completed_now
        window = MainWindow(root, settings, background)
    except Exception as exc:
        QMessageBox.critical(None, "AnalyticsBot", f"Не удалось открыть приложение: {friendly_error(exc)}")
        return 1
    if not background or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    return app.exec()
