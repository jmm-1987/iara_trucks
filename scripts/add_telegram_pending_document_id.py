"""
Script de migración: agregar campo pending_document_id a telegram_session.
"""
import sqlite3
import sys
from pathlib import Path

# Ruta a la base de datos
db_path = Path(__file__).parent.parent / "instance" / "fleet_management.db"

if not db_path.exists():
    print(f"[ERROR] Base de datos no encontrada en {db_path}")
    sys.exit(1)

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

try:
    # Verificar si la columna ya existe
    cursor.execute("PRAGMA table_info(telegram_session)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "pending_document_id" in columns:
        print("[OK] La columna pending_document_id ya existe en telegram_session")
    else:
        # Agregar la columna
        cursor.execute("""
            ALTER TABLE telegram_session 
            ADD COLUMN pending_document_id INTEGER REFERENCES document(id)
        """)
        conn.commit()
        print("[OK] Columna pending_document_id agregada a telegram_session")
    
    conn.close()
    print("[OK] Migracion completada")
except Exception as e:
    conn.rollback()
    conn.close()
    print(f"[ERROR] Error: {e}")
    sys.exit(1)

