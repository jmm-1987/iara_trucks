"""Tests de modelos."""
from app.models import Document, Vehicle, db


def test_create_vehicle(db_session, app):
    with app.app_context():
        v = Vehicle(plate="1234ABC", alias="Camión 1", brand="Iveco", model="Daily")
        db.session.add(v)
        db.session.commit()
        assert v.id is not None
        assert v.plate == "1234ABC"
        assert v.active is True


def test_create_document(db_session, app):
    with app.app_context():
        v = Vehicle(plate="1234ABC")
        db.session.add(v)
        db.session.commit()
        doc = Document(
            vehicle_id=v.id,
            file_path="test.jpg",
            status="pending",
        )
        db.session.add(doc)
        db.session.commit()
        assert doc.id is not None
        assert doc.status == "pending"
