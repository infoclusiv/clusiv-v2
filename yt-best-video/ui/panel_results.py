import pyperclip
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QGroupBox,
    QMessageBox,
    QComboBox,
)


class PanelResults(QWidget):
    automation_requested = pyqtSignal()
    refresh_journeys_requested = pyqtSignal()
    journey_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._journeys_signature = []
        self._automation_busy = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.lbl_estado = QLabel("Listo")
        self.lbl_estado.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_estado)

        group = QGroupBox("Video Ganador")
        inner = QVBoxLayout()

        self.lbl_titulo = QLabel("—")
        self.lbl_titulo.setWordWrap(True)
        self.lbl_titulo.setStyleSheet("font-size: 18px; font-weight: bold;")
        inner.addWidget(self.lbl_titulo)

        self.lbl_detalle = QLabel("")
        self.lbl_detalle.setWordWrap(True)
        inner.addWidget(self.lbl_detalle)

        self.btn_copiar = QPushButton("Copiar título")
        self.btn_copiar.clicked.connect(self._on_copiar)
        self.btn_copiar.setEnabled(False)
        inner.addWidget(self.btn_copiar)

        group.setLayout(inner)
        layout.addWidget(group)

        automation_group = QGroupBox("Automatización extensión")
        automation_layout = QVBoxLayout()

        self.lbl_extension_status = QLabel(
            "Canal principal: desconectado | Variables: desconectado"
        )
        self.lbl_extension_status.setWordWrap(True)
        automation_layout.addWidget(self.lbl_extension_status)

        journey_row = QHBoxLayout()
        self.cmb_journeys = QComboBox()
        self.cmb_journeys.currentIndexChanged.connect(self._on_journey_changed)
        journey_row.addWidget(self.cmb_journeys, stretch=1)

        self.btn_refrescar_journeys = QPushButton("Actualizar journeys")
        self.btn_refrescar_journeys.clicked.connect(self.refresh_journeys_requested.emit)
        journey_row.addWidget(self.btn_refrescar_journeys)
        automation_layout.addLayout(journey_row)

        self.btn_automatizar = QPushButton("Analizar + Ejecutar Journey")
        self.btn_automatizar.clicked.connect(self.automation_requested.emit)
        automation_layout.addWidget(self.btn_automatizar)

        self.lbl_automation_status = QLabel(
            "Selecciona un journey y espera a que la extensión conecte."
        )
        self.lbl_automation_status.setWordWrap(True)
        automation_layout.addWidget(self.lbl_automation_status)

        automation_group.setLayout(automation_layout)
        layout.addWidget(automation_group)
        layout.addStretch()

        self._titulo_actual = ""
        self.set_journeys([], "")
        self._update_automation_button_state()

    def mostrar_resultado(self, titulo, views, ch_name):
        self.lbl_estado.setText("Análisis completado")
        self.lbl_titulo.setText(titulo)
        self.lbl_detalle.setText(f"Vistas: {views:,}  |  Canal: {ch_name}")
        self._titulo_actual = titulo
        self.btn_copiar.setEnabled(True)

    def mostrar_error(self, mensaje):
        self.lbl_estado.setText("Error")
        self.lbl_titulo.setText("—")
        self.lbl_detalle.setText(mensaje)
        self._titulo_actual = ""
        self.btn_copiar.setEnabled(False)

    def mostrar_estado(self, texto):
        self.lbl_estado.setText(texto)

    def set_connection_status(self, control_connected, template_connected):
        principal = "conectado" if control_connected else "desconectado"
        variables = "conectado" if template_connected else "desconectado"
        self.lbl_extension_status.setText(
            f"Canal principal: {principal} | Variables: {variables}"
        )

    def set_journeys(self, journeys, selected_journey_id):
        signature = [
            (journey.get("id", ""), journey.get("name", "")) for journey in journeys
        ]
        if signature == self._journeys_signature and (
            selected_journey_id == (self.cmb_journeys.currentData() or "")
        ):
            return

        self._journeys_signature = signature
        self.cmb_journeys.blockSignals(True)
        self.cmb_journeys.clear()
        self.cmb_journeys.addItem("Selecciona un journey...", "")

        selected_index = 0
        for index, journey in enumerate(journeys, start=1):
            label = f"{journey.get('name', 'Sin nombre')} ({len(journey.get('steps') or [])} pasos)"
            journey_id = journey.get("id", "")
            self.cmb_journeys.addItem(label, journey_id)
            if journey_id and journey_id == selected_journey_id:
                selected_index = index

        self.cmb_journeys.setCurrentIndex(selected_index)
        self.cmb_journeys.blockSignals(False)
        self._update_automation_button_state()

    def set_automation_status(self, texto):
        self.lbl_automation_status.setText(texto)

    def set_busy(self, is_busy):
        self._automation_busy = is_busy
        self.btn_refrescar_journeys.setEnabled(not is_busy)
        self.cmb_journeys.setEnabled(not is_busy)
        self._update_automation_button_state()

    def selected_journey_id(self):
        return self.cmb_journeys.currentData() or ""

    def _update_automation_button_state(self):
        has_selection = bool(self.cmb_journeys.currentData())
        self.btn_automatizar.setEnabled(has_selection and not self._automation_busy)

    def _on_journey_changed(self):
        self._update_automation_button_state()
        self.journey_selected.emit(self.selected_journey_id())

    def _on_copiar(self):
        if self._titulo_actual:
            pyperclip.copy(self._titulo_actual)
            QMessageBox.information(self, "Copiado", "Título copiado al portapapeles.")
