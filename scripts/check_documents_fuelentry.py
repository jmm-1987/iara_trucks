"""
Script para verificar la relación entre Documentos y FuelEntry para 3130LDW.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.models import Vehicle, FuelEntry, Document, db

app = create_app()

with app.app_context():
    print("=" * 60)
    print("VERIFICACION DOCUMENTOS vs FUELENTRY PARA 3130LDW")
    print("=" * 60)
    
    vehicle = Vehicle.query.filter_by(plate="3130LDW").first()
    if not vehicle:
        print("Vehículo no encontrado")
        sys.exit(1)
    
    print(f"\nVehículo: {vehicle.plate} (ID: {vehicle.id})")
    
    # Todos los documentos del vehículo
    print("\n" + "-" * 60)
    print("DOCUMENTOS:")
    documents = Document.query.filter_by(vehicle_id=vehicle.id).order_by(Document.id.desc()).all()
    print(f"Total documentos: {len(documents)}")
    
    for doc in documents:
        print(f"\n  Documento ID: {doc.id}")
        print(f"    Tipo: {doc.doc_type}")
        print(f"    Estado: {doc.status}")
        print(f"    Fecha emisión: {doc.issue_date}")
        print(f"    Subtotal: {doc.subtotal_amount} €")
        print(f"    IVA: {doc.tax_amount} €")
        print(f"    Total: {doc.total_amount} €")
        print(f"    Fecha subida: {doc.uploaded_at}")
    
    # Todos los FuelEntry del vehículo
    print("\n" + "-" * 60)
    print("FUELENTRY:")
    fuel_entries = FuelEntry.query.filter_by(vehicle_id=vehicle.id).order_by(FuelEntry.date.asc()).all()
    print(f"Total FuelEntry: {len(fuel_entries)}")
    
    for fe in fuel_entries:
        print(f"\n  FuelEntry ID: {fe.id}")
        print(f"    Document ID: {fe.document_id}")
        print(f"    Fecha: {fe.date}")
        print(f"    Litros: {fe.liters} L")
        print(f"    Subtotal: {fe.subtotal_amount} €")
        print(f"    IVA: {fe.tax_amount} €")
        print(f"    Total: {fe.total_amount} €")
        
        # Verificar si el documento existe
        if fe.document_id:
            doc = Document.query.get(fe.document_id)
            if doc:
                print(f"    [OK] Documento existe: ID {doc.id}, tipo {doc.doc_type}, estado {doc.status}")
            else:
                print(f"    [ERROR] Documento NO existe (document_id={fe.document_id})")
        else:
            print(f"    [WARNING] FuelEntry sin documento asociado (document_id=None)")
    
    # Verificar documentos sin FuelEntry
    print("\n" + "-" * 60)
    print("DOCUMENTOS SIN FUELENTRY:")
    docs_without_fuel = []
    for doc in documents:
        if doc.doc_type == "fuel_ticket":
            fuel_entry = FuelEntry.query.filter_by(document_id=doc.id).first()
            if not fuel_entry:
                docs_without_fuel.append(doc)
                print(f"  Documento ID {doc.id}: fuel_ticket sin FuelEntry")
    
    if not docs_without_fuel:
        print("  Todos los documentos fuel_ticket tienen FuelEntry asociado")
    
    print("=" * 60)

