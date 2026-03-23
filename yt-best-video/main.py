import sys
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QPushButton,
    QMessageBox,
)
from PyQt6.QtCore import QThread, QTimer, pyqtSignal

from config import (
    EXTENSION_CONNECTION_TIMEOUT_SECONDS,
    EXTENSION_CONTROL_WS_PORT,
    EXTENSION_TEMPLATE_WS_PORT,
    EXTENSION_WS_HOST,
    JOURNEY_EXECUTION_TIMEOUT_SECONDS,
    JOURNEY_VALIDATION_TIMEOUT_SECONDS,
    YOUTUBE_API_KEY,
)
from database import obtener_canales_db
from extension_bridge import ExtensionBridgeServer
import logger as log
from youtube_analyzer import analizar_rendimiento_canal
from ui.state import AppState
from ui.panel_channels import PanelChannels
from ui.panel_results import PanelResults


CHATGPT_TAB_PATTERNS = [
    "https://chatgpt.com/*",
    "https://chat.openai.com/*",
]


def analizar_canales(canales, progreso_callback):
    ganadores = []
    for canal in canales:
        progreso_callback(f"Analizando: {canal['nombre']}...")
        resultado = analizar_rendimiento_canal(canal["canal_id"], canal["nombre"])
        if resultado:
            ganadores.append(resultado)

    if not ganadores:
        raise RuntimeError("No se encontraron videos recientes en ningún canal.")

    return max(ganadores, key=lambda x: x["views"])


class WorkerAnalisis(QThread):
    resultado = pyqtSignal(dict)
    error = pyqtSignal(str)
    progreso = pyqtSignal(str)

    def __init__(self, canales, parent=None):
        super().__init__(parent)
        self.canales = canales

    def run(self):
        try:
            mejor = analizar_canales(self.canales, self.progreso.emit)
        except RuntimeError as exc:
            self.error.emit(str(exc))
            return

        self.resultado.emit(mejor)


class WorkerAutomatizacion(QThread):
    resultado = pyqtSignal(dict)
    error = pyqtSignal(str)
    progreso = pyqtSignal(str)

    def __init__(self, canales, bridge, journey_id, parent=None):
        super().__init__(parent)
        self.canales = canales
        self.bridge = bridge
        self.journey_id = journey_id

    def run(self):
        execution_context = {
            "journey_id": self.journey_id,
            "canal_count": len(self.canales),
        }

        log.journey_event("worker_automatizacion_start", **execution_context)

        log.info("step_wait_connections", **execution_context)
        self.progreso.emit("Esperando conexión de la extensión...")
        ok, error = self.bridge.wait_for_connections(
            timeout=EXTENSION_CONNECTION_TIMEOUT_SECONDS
        )
        if not ok:
            log.error("step_wait_connections_failed", error=error, **execution_context)
            self.error.emit(error)
            return
        log.info("step_wait_connections_ok", **execution_context)

        log.info("step_prepare_tab", **execution_context)
        self.progreso.emit("Preparando pestaña de ChatGPT...")
        ok, error = self.bridge.prepare_chatgpt_tab(
            timeout=EXTENSION_CONNECTION_TIMEOUT_SECONDS,
            tab_url_patterns=CHATGPT_TAB_PATTERNS,
        )
        if not ok:
            log.error("step_prepare_tab_failed", error=error, **execution_context)
            self.error.emit(error)
            return
        log.info("step_prepare_tab_ok", tab_message=error, **execution_context)

        log.info("step_check_journey_exists", **execution_context)
        self.progreso.emit("Consultando journeys disponibles...")
        self.bridge.request_journeys()
        self.bridge.wait_for_journeys(timeout=4.0)
        journeys = self.bridge.get_available_journeys()
        journey_ids = [journey.get("id") for journey in journeys]
        log.info(
            "step_journeys_received",
            count=len(journeys),
            journey_ids=journey_ids,
            looking_for=self.journey_id,
            **execution_context,
        )
        if not any(journey.get("id") == self.journey_id for journey in journeys):
            log.error(
                "step_journey_not_found",
                available_ids=journey_ids,
                **execution_context,
            )
            self.error.emit(
                "El journey seleccionado ya no existe en la extensión. Actualiza la lista y vuelve a elegirlo."
            )
            return
        log.info("step_journey_found", **execution_context)

        log.info("step_analyze_channels", **execution_context)
        try:
            mejor = analizar_canales(self.canales, self.progreso.emit)
        except RuntimeError as exc:
            log.error("step_analyze_channels_failed", error=str(exc), **execution_context)
            self.error.emit(str(exc))
            return

        log.info(
            "step_analyze_channels_ok",
            winner_title=mejor.get("title"),
            winner_views=mejor.get("views"),
            winner_channel=mejor.get("ch_name"),
            **execution_context,
        )

        self.resultado.emit(mejor)

        log.info("step_sync_ref_title", title=mejor["title"], **execution_context)
        self.progreso.emit("Sincronizando REF_TITLE con la extensión...")
        ok, error = self.bridge.sync_ref_title(
            mejor["title"],
            {
                "channel": mejor["ch_name"],
                "views": mejor["views"],
                "video_id": mejor.get("video_id"),
            },
        )
        if not ok:
            log.error("step_sync_ref_title_failed", error=error, **execution_context)
            self.error.emit(error)
            return
        log.info("step_sync_ref_title_ok", **execution_context)

        log.info("step_validate_journey", **execution_context)
        self.progreso.emit("Validando variables, textos y sitio antes de ejecutar...")
        ok, error, validation = self.bridge.validate_journey_execution(
            self.journey_id,
            timeout=JOURNEY_VALIDATION_TIMEOUT_SECONDS,
            tab_url_patterns=CHATGPT_TAB_PATTERNS,
        )
        log.info(
            "step_validate_journey_result",
            ok=ok,
            error=error,
            validation_status=validation.get("status") if validation else None,
            missing_variables=validation.get("missing_variables") if validation else None,
            missing_texts=validation.get("missing_texts") if validation else None,
            page=validation.get("page") if validation else None,
            **execution_context,
        )
        if not ok:
            details = []
            if validation:
                missing_variables = validation.get("missing_variables") or []
                missing_texts = validation.get("missing_texts") or []
                page = validation.get("page") or {}
                if missing_variables:
                    details.append(
                        "Variables faltantes: " + ", ".join(missing_variables)
                    )
                if missing_texts:
                    details.append(
                        "Textos faltantes: " + ", ".join(missing_texts)
                    )
                if page.get("url"):
                    details.append(f"Sitio validado: {page['url']}")

            if details:
                error = f"{error}\n" + "\n".join(details)

            log.error("step_validate_journey_failed", error=error, **execution_context)
            self.error.emit(error)
            return

        log.journey_event("step_run_journey_start", **execution_context)
        self.progreso.emit("Ejecutando journey 'Generar Titulo'...")
        ok, error, execution_id = self.bridge.run_journey(
            self.journey_id,
            tab_url_patterns=CHATGPT_TAB_PATTERNS,
        )
        if not ok:
            log.error(
                "step_run_journey_dispatch_failed",
                error=error,
                **execution_context,
            )
            self.error.emit(error)
            return

        log.journey_event(
            "step_run_journey_dispatched",
            execution_id=execution_id,
            **execution_context,
        )

        log.journey_event(
            "step_wait_completion_start",
            execution_id=execution_id,
            timeout=JOURNEY_EXECUTION_TIMEOUT_SECONDS,
            **execution_context,
        )
        ok, error = self.bridge.wait_for_journey_completion(
            execution_id,
            timeout=JOURNEY_EXECUTION_TIMEOUT_SECONDS,
            progress_callback=self.progreso.emit,
        )
        if not ok:
            log.error(
                "step_wait_completion_failed",
                execution_id=execution_id,
                error=error,
                **execution_context,
            )
            self.error.emit(error)
            return

        log.journey_event(
            "step_wait_completion_ok",
            execution_id=execution_id,
            **execution_context,
        )
        self.progreso.emit("Journey completado correctamente.")


class VentanaPrincipal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Best Video Extractor")
        self.setMinimumSize(900, 550)

        self.state = AppState()
        self.worker = None
        self._worker_kind: Optional[str] = None
        self._last_control_connected = False
        self.extension_bridge = ExtensionBridgeServer(
            EXTENSION_WS_HOST,
            EXTENSION_CONTROL_WS_PORT,
            EXTENSION_TEMPLATE_WS_PORT,
        )
        self.extension_bridge.start()

        self._build_ui()
        self._setup_status_timer()
        self._refresh_extension_state()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout_principal = QVBoxLayout(central)

        layout_columnas = QHBoxLayout()

        self.panel_channels = PanelChannels()
        self.panel_channels.canales_changed.connect(self._on_canales_changed)
        layout_columnas.addWidget(self.panel_channels, stretch=1)

        self.panel_results = PanelResults()
        self.panel_results.refresh_journeys_requested.connect(
            self._on_refresh_journeys_requested
        )
        self.panel_results.automation_requested.connect(self._on_automatizar)
        self.panel_results.journey_selected.connect(self._on_journey_selected)
        layout_columnas.addWidget(self.panel_results, stretch=1)

        layout_principal.addLayout(layout_columnas)

        self.btn_analizar = QPushButton("Solo Analizar Mejor Video")
        self.btn_analizar.setMinimumHeight(45)
        self.btn_analizar.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.btn_analizar.clicked.connect(self._on_analizar)
        layout_principal.addWidget(self.btn_analizar)

        self.panel_channels.refrescar_canales()

    def _setup_status_timer(self):
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(1000)
        self.status_timer.timeout.connect(self._refresh_extension_state)
        self.status_timer.start()

    def _refresh_extension_state(self):
        connection_state = self.extension_bridge.get_connection_state()
        control_connected = connection_state["control_connected"]
        template_connected = connection_state["template_connected"]
        self.panel_results.set_connection_status(control_connected, template_connected)

        if control_connected and not self._last_control_connected:
            self.extension_bridge.request_journeys()

        journeys = self.extension_bridge.get_available_journeys()
        journey_ids = {journey.get("id") for journey in journeys if journey.get("id")}
        if journeys and self.state.selected_journey_id and self.state.selected_journey_id not in journey_ids:
            self.state.set_selected_journey("", "")
            self.panel_results.set_automation_status(
                "El journey guardado ya no existe. Selecciona uno nuevo."
            )

        self.panel_results.set_journeys(journeys, self.state.selected_journey_id)
        self._last_control_connected = control_connected

    def _on_canales_changed(self):
        self.state.canales = obtener_canales_db()

    def _on_analizar(self):
        canales = self._validar_requisitos_basicos()
        if canales is None or self._worker_en_ejecucion():
            return

        self._iniciar_worker(WorkerAnalisis(canales), "manual")

    def _on_automatizar(self):
        canales = self._validar_requisitos_basicos(require_journey=True)
        if canales is None or self._worker_en_ejecucion():
            return

        self.panel_results.set_automation_status(
            "Preparando pestaña de ChatGPT desde la extensión..."
        )

        self._iniciar_worker(
            WorkerAutomatizacion(
                canales,
                self.extension_bridge,
                self.state.selected_journey_id,
            ),
            "automation",
        )

    def _iniciar_worker(self, worker, worker_kind):
        self.worker = worker
        self._worker_kind = worker_kind
        self.btn_analizar.setEnabled(False)
        self.panel_results.set_busy(True)
        self.panel_results.mostrar_estado("Analizando...")
        self.worker.resultado.connect(self._on_resultado)
        self.worker.error.connect(self._on_error)
        self.worker.progreso.connect(self._on_progreso)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _worker_en_ejecucion(self):
        return bool(self.worker and self.worker.isRunning())

    def _validar_requisitos_basicos(self, require_journey=False):
        if not YOUTUBE_API_KEY:
            QMessageBox.critical(
                self,
                "Error de configuración",
                "No se encontró YOUTUBE_API_KEY en .env\n"
                "Configure su clave de API de YouTube.",
            )
            return None

        canales = obtener_canales_db()
        if not canales:
            QMessageBox.warning(
                self,
                "Sin canales",
                "Agregue al menos un canal de YouTube antes de analizar.",
            )
            return None

        if require_journey and not self.state.selected_journey_id:
            QMessageBox.warning(
                self,
                "Journey no configurado",
                "Selecciona el journey que debe ejecutar la extensión antes de continuar.",
            )
            return None

        return canales

    def _on_refresh_journeys_requested(self):
        ok, error = self.extension_bridge.request_journeys()
        if not ok:
            self.panel_results.set_automation_status(error)
            return

        self.panel_results.set_automation_status("Solicitando journeys a la extensión...")

    def _on_journey_selected(self, journey_id):
        journey_name = ""
        for journey in self.extension_bridge.get_available_journeys():
            if journey.get("id") == journey_id:
                journey_name = journey.get("name", "")
                break

        self.state.set_selected_journey(journey_id, journey_name)

    def _on_resultado(self, resultado):
        self.state.resultado_ganador = resultado
        self.panel_results.mostrar_resultado(
            resultado["title"],
            resultado["views"],
            resultado["ch_name"],
        )
        if self._worker_kind == "manual":
            self._sync_resultado_con_extension(resultado)

    def _on_error(self, mensaje):
        if self._worker_kind == "automation" and self.state.resultado_ganador:
            self.panel_results.mostrar_estado("Error en automatización")
            self.panel_results.set_automation_status(mensaje)
            return

        self.panel_results.mostrar_error(mensaje)
        if self._worker_kind == "automation":
            self.panel_results.set_automation_status(mensaje)

    def _on_progreso(self, texto):
        if self._worker_kind == "automation":
            self.panel_results.set_automation_status(texto)
        else:
            self.panel_results.mostrar_estado(texto)

    def _on_finished(self):
        self.btn_analizar.setEnabled(True)
        self.panel_results.set_busy(False)
        self.worker = None
        self._worker_kind = None

    def _sync_resultado_con_extension(self, resultado):
        ok, error = self.extension_bridge.sync_ref_title(
            resultado["title"],
            {
                "channel": resultado["ch_name"],
                "views": resultado["views"],
                "video_id": resultado.get("video_id"),
            },
        )
        if not ok:
            print(f"No se pudo sincronizar REF_TITLE con la extension: {error}")
            self.panel_results.set_automation_status(error)

    def closeEvent(self, event):
        self.extension_bridge.stop()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    ventana = VentanaPrincipal()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
