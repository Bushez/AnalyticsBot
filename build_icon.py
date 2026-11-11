import os
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from analyticsbot.gui import icon


app = QApplication([])
if not icon().pixmap(64, 64).save(str(Path("gui-build") / "analyticsbot.ico"), "ICO"):
    raise RuntimeError("Could not create application icon")
