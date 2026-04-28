"""
Compatibilidad de esquema SQLite sin migraciones formales.
"""

from sqlalchemy import text

from app.models import db


def _column_exists(table_name: str, column_name: str) -> bool:
    rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    for row in rows:
        if len(row) > 1 and row[1] == column_name:
            return True
    return False


def ensure_schema_compatibility() -> None:
    """Añade columnas nuevas en instalaciones ya existentes."""
    if not _column_exists("reminder", "title"):
        db.session.execute(text("ALTER TABLE reminder ADD COLUMN title VARCHAR(255)"))
    if not _column_exists("reminder", "notes"):
        db.session.execute(text("ALTER TABLE reminder ADD COLUMN notes TEXT"))
    if not _column_exists("reminder", "notify_days_before"):
        db.session.execute(text("ALTER TABLE reminder ADD COLUMN notify_days_before INTEGER"))
    if not _column_exists("reminder", "last_notified_days_remaining"):
        db.session.execute(
            text("ALTER TABLE reminder ADD COLUMN last_notified_days_remaining INTEGER")
        )
    if not _column_exists("reminder", "last_notified_at"):
        db.session.execute(text("ALTER TABLE reminder ADD COLUMN last_notified_at DATETIME"))
    if not _column_exists("document", "file_hash"):
        db.session.execute(text("ALTER TABLE document ADD COLUMN file_hash VARCHAR(64)"))
    db.session.commit()
