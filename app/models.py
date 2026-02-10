"""
Modelos SQLAlchemy - Gestión de Flotas
"""
from datetime import datetime
from enum import Enum as PyEnum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.orm import relationship

db = SQLAlchemy()


class DocumentType(PyEnum):
    """Tipos de documento soportados."""

    FUEL_TICKET = "fuel_ticket"
    INSURANCE_POLICY = "insurance_policy"
    ITV = "itv"
    TACHOGRAPH = "tachograph"
    WORKSHOP_INVOICE = "workshop_invoice"
    TIRES_INVOICE = "tires_invoice"
    OTHER = "other"


class DocumentStatus(PyEnum):
    """Estado del procesamiento del documento."""

    PENDING = "pending"
    PROCESSED = "processed"
    ERROR = "error"


class ExpenseCategory(PyEnum):
    """Categorías de gastos."""

    WORKSHOP = "workshop"
    TIRES = "tires"
    INSURANCE = "insurance"
    ITV = "itv"
    OTHER = "other"


class ReminderKind(PyEnum):
    """Tipos de recordatorio."""

    INSURANCE = "insurance"
    ITV = "itv"
    TACHOGRAPH = "tachograph"


class User(db.Model):
    """Usuario de Telegram."""

    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    documents = relationship("Document", back_populates="user")
    sessions = relationship("TelegramSession", back_populates="user", uselist=False)


class Vehicle(db.Model):
    """Vehículo de la flota."""

    __tablename__ = "vehicle"

    id = db.Column(db.Integer, primary_key=True)
    plate = db.Column(db.String(20), unique=True, nullable=False, index=True)
    alias = db.Column(db.String(100))
    brand = db.Column(db.String(100))
    model = db.Column(db.String(100))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    documents = relationship("Document", back_populates="vehicle")
    fuel_entries = relationship("FuelEntry", back_populates="vehicle")
    expense_entries = relationship("ExpenseEntry", back_populates="vehicle")
    reminders = relationship("Reminder", back_populates="vehicle")


class Document(db.Model):
    """Documento subido (imagen/pdf) asociado a un vehículo."""

    __tablename__ = "document"

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, ForeignKey("vehicle.id"), nullable=True)
    user_id = db.Column(db.Integer, ForeignKey("user.id"), nullable=True)
    doc_type = db.Column(db.String(50))  # DocumentType value
    file_path = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(
        db.String(20), default=DocumentStatus.PENDING.value
    )  # pending/processed/error
    raw_text = db.Column(Text, nullable=True)
    extracted_json = db.Column(Text, nullable=True)
    error_message = db.Column(Text, nullable=True)

    # Campos extraídos
    vendor = db.Column(db.String(255))
    issue_date = db.Column(db.Date, nullable=True)
    due_date = db.Column(db.Date, nullable=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(3), default="EUR")
    odometer_km = db.Column(db.Integer, nullable=True)

    vehicle = relationship("Vehicle", back_populates="documents")
    user = relationship("User", back_populates="documents")
    fuel_entry = relationship("FuelEntry", back_populates="document", uselist=False)
    expense_entry = relationship(
        "ExpenseEntry", back_populates="document", uselist=False
    )


class FuelEntry(db.Model):
    """Entrada de combustible (ticket gasoil)."""

    __tablename__ = "fuel_entry"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, ForeignKey("document.id"), nullable=True)
    vehicle_id = db.Column(db.Integer, ForeignKey("vehicle.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    liters = db.Column(db.Numeric(10, 2), nullable=False)
    price_per_liter = db.Column(db.Numeric(8, 4), nullable=False)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    station = db.Column(db.String(255))
    fuel_type = db.Column(db.String(50))

    document = relationship("Document", back_populates="fuel_entry")
    vehicle = relationship("Vehicle", back_populates="fuel_entries")


class ExpenseEntry(db.Model):
    """Entrada de gasto (taller, neumáticos, seguro, ITV, etc.)."""

    __tablename__ = "expense_entry"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, ForeignKey("document.id"), nullable=True)
    vehicle_id = db.Column(db.Integer, ForeignKey("vehicle.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(50), nullable=False)  # ExpenseCategory value
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    vendor = db.Column(db.String(255))

    document = relationship("Document", back_populates="expense_entry")
    vehicle = relationship("Vehicle", back_populates="expense_entries")


class Reminder(db.Model):
    """Recordatorio de vencimiento (seguro, ITV, tacógrafo)."""

    __tablename__ = "reminder"

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, ForeignKey("vehicle.id"), nullable=False)
    kind = db.Column(db.String(50), nullable=False)  # ReminderKind value
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default="active")  # active, notified, expired
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    document_id = db.Column(db.Integer, ForeignKey("document.id"), nullable=True)

    vehicle = relationship("Vehicle", back_populates="reminders")


class TelegramSession(db.Model):
    """Sesión de usuario en Telegram (vehículo seleccionado actual)."""

    __tablename__ = "telegram_session"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, ForeignKey("user.id"), nullable=False, unique=True)
    current_vehicle_id = db.Column(db.Integer, ForeignKey("vehicle.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="sessions")
    current_vehicle = relationship("Vehicle")
