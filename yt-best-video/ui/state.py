from PyQt6.QtCore import QSettings

from database import init_db, obtener_canales_db


class AppState:
    def __init__(self):
        init_db()
        self.settings = QSettings("Clusiv", "YTBestVideo")
        self.canales = obtener_canales_db()
        self.resultado_ganador = None
        self.selected_journey_id = (
            self.settings.value("automation/selected_journey_id", "", str) or ""
        )
        self.selected_journey_name = (
            self.settings.value("automation/selected_journey_name", "", str) or ""
        )

    def set_selected_journey(self, journey_id, journey_name=""):
        self.selected_journey_id = journey_id or ""
        self.selected_journey_name = journey_name or ""
        self.settings.setValue("automation/selected_journey_id", self.selected_journey_id)
        self.settings.setValue(
            "automation/selected_journey_name", self.selected_journey_name
        )
