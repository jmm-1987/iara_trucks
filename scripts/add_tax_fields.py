#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de migración: añade campos subtotal_amount y tax_amount a las tablas.
Ejecutar: python scripts/add_tax_fields.py
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
    """Añade las nuevas columnas de subtotal e IVA."""
    app = create_app()
    with app.app_context():
        # Añadir columnas a document
        try:
            db.session.execute(text("""
                ALTER TABLE document 
                ADD COLUMN subtotal_amount NUMERIC(12, 2)
            """))
            print("[OK] Columna subtotal_amount añadida a document")
        except Exception as e:
            print(f"[WARN] subtotal_amount en document: {e}")
        
        try:
            db.session.execute(text("""
                ALTER TABLE document 
                ADD COLUMN tax_amount NUMERIC(12, 2)
            """))
            print("[OK] Columna tax_amount añadida a document")
        except Exception as e:
            print(f"[WARN] tax_amount en document: {e}")
        
        # Añadir columnas a fuel_entry
        try:
            db.session.execute(text("""
                ALTER TABLE fuel_entry 
                ADD COLUMN subtotal_amount NUMERIC(12, 2)
            """))
            print("[OK] Columna subtotal_amount añadida a fuel_entry")
        except Exception as e:
            print(f"[WARN] subtotal_amount en fuel_entry: {e}")
        
        try:
            db.session.execute(text("""
                ALTER TABLE fuel_entry 
                ADD COLUMN tax_amount NUMERIC(12, 2)
            """))
            print("[OK] Columna tax_amount añadida a fuel_entry")
        except Exception as e:
            print(f"[WARN] tax_amount en fuel_entry: {e}")
        
        # Añadir columnas a expense_entry
        try:
            db.session.execute(text("""
                ALTER TABLE expense_entry 
                ADD COLUMN subtotal_amount NUMERIC(12, 2)
            """))
            print("[OK] Columna subtotal_amount añadida a expense_entry")
        except Exception as e:
            print(f"[WARN] subtotal_amount en expense_entry: {e}")
        
        try:
            db.session.execute(text("""
                ALTER TABLE expense_entry 
                ADD COLUMN tax_amount NUMERIC(12, 2)
            """))
            print("[OK] Columna tax_amount añadida a expense_entry")
        except Exception as e:
            print(f"[WARN] tax_amount en expense_entry: {e}")
        
        db.session.commit()
        print("\n[OK] Migracion completada")

if __name__ == "__main__":
    migrate_database()

