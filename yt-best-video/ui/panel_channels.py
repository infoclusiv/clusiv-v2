from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QGroupBox,
)
from PyQt6.QtCore import pyqtSignal
from database import agregar_canal_db, eliminar_canal_db, obtener_canales_db


class PanelChannels(QWidget):
    canales_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        group = QGroupBox("Agregar Canal YouTube")
        form = QVBoxLayout()

        self.input_canal_id = QLineEdit()
        self.input_canal_id.setPlaceholderText("ID del canal (ej: UCxxxxxx)")
        form.addWidget(QLabel("Canal ID:"))
        form.addWidget(self.input_canal_id)

        self.input_nombre = QLineEdit()
        self.input_nombre.setPlaceholderText("Nombre del canal")
        form.addWidget(QLabel("Nombre:"))
        form.addWidget(self.input_nombre)

        btn_agregar = QPushButton("Agregar Canal")
        btn_agregar.clicked.connect(self._on_agregar)
        form.addWidget(btn_agregar)

        group.setLayout(form)
        layout.addWidget(group)

        group_lista = QGroupBox("Canales Configurados")
        lista_layout = QVBoxLayout()

        self.lista_canales = QListWidget()
        lista_layout.addWidget(self.lista_canales)

        btn_eliminar = QPushButton("Eliminar Seleccionado")
        btn_eliminar.clicked.connect(self._on_eliminar)
        lista_layout.addWidget(btn_eliminar)

        group_lista.setLayout(lista_layout)
        layout.addWidget(group_lista)

    def _on_agregar(self):
        canal_id = self.input_canal_id.text().strip()
        nombre = self.input_nombre.text().strip()
        if not canal_id or not nombre:
            QMessageBox.warning(
                self, "Campos vacíos", "Debe ingresar ID y nombre del canal."
            )
            return
        agregar_canal_db(canal_id, nombre)
        self.input_canal_id.clear()
        self.input_nombre.clear()
        self.refrescar_canales()
        self.canales_changed.emit()

    def _on_eliminar(self):
        item = self.lista_canales.currentItem()
        if not item:
            return
        canal_id = item.data(256)
        eliminar_canal_db(canal_id)
        self.refrescar_canales()
        self.canales_changed.emit()

    def refrescar_canales(self):
        self.lista_canales.clear()
        canales = obtener_canales_db()
        for c in canales:
            display = f"{c['nombre']}  ({c['canal_id']})"
            item = QListWidgetItem(display)
            item.setData(256, c["canal_id"])
            self.lista_canales.addItem(item)
