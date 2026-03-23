import sqlite3
from config import DATABASE_FILE


def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS canales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal_id TEXT NOT NULL UNIQUE,
            nombre TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def agregar_canal_db(canal_id, nombre):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO canales (canal_id, nombre) VALUES (?, ?)",
        (canal_id, nombre),
    )
    conn.commit()
    conn.close()


def eliminar_canal_db(canal_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM canales WHERE canal_id = ?", (canal_id,))
    conn.commit()
    conn.close()


def obtener_canales_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT canal_id, nombre FROM canales")
    canales = [{"canal_id": row[0], "nombre": row[1]} for row in cursor.fetchall()]
    conn.close()
    return canales
