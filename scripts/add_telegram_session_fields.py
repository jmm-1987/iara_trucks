#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de migración: añade campos a telegram_session para manejar estados.
Ejecutar: python scripts/add_telegram_session_fields.py
"""
import os
import sys

os.environ.setdefault("ENABLE_SCHEDULER", "0")
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app
from app.models import db
from sqlalchemy import text

def migrate_database():
    """Añade campos a telegram_session."""
    app = create_app()
    with app.app_context():
        try:
            db.session.execute(text("""
                ALTER TABLE telegram_session 
                ADD COLUMN pending_action VARCHAR(50)
            """))
            print("[OK] Columna pending_action añadida")
        except Exception as e:
            print(f"[WARN] pending_action: {e}")
            db.session.rollback()
        
        try:
            db.session.execute(text("""
                ALTER TABLE telegram_session 
                ADD COLUMN pending_vehicle_id INTEGER REFERENCES vehicle(id)
            """))
            print("[OK] Columna pending_vehicle_id añadida")
        except Exception as e:
            print(f"[WARN] pending_vehicle_id: {e}")
            db.session.rollback()
        
        try:
            db.session.execute(text("""
                ALTER TABLE telegram_session 
                ADD COLUMN pending_file_id VARCHAR(255)
            """))
            print("[OK] Columna pending_file_id añadida")
        except Exception as e:
            print(f"[WARN] pending_file_id: {e}")
            db.session.rollback()
        
        try:
            db.session.execute(text("""
                ALTER TABLE telegram_session 
                ADD COLUMN pending_file_path VARCHAR(255)
            """))
            print("[OK] Columna pending_file_path añadida")
        except Exception as e:
            print(f"[WARN] pending_file_path: {e}")
            db.session.rollback()
        
        db.session.commit()
        print("\n[OK] Migracion completada")

if __name__ == "__main__":
    migrate_database()

