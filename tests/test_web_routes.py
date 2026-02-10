"""Tests de rutas web."""
import pytest


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200


def test_vehicle_list(client):
    r = client.get("/vehiculos")
    assert r.status_code == 200


def test_document_list(client):
    r = client.get("/documentos")
    assert r.status_code == 200


def test_reports(client):
    r = client.get("/reportes")
    assert r.status_code == 200


def test_reminders(client):
    r = client.get("/recordatorios")
    assert r.status_code == 200
