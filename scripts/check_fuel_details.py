"""
Script para verificar los detalles de FuelEntry, especialmente subtotal_amount y tax_amount.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.models import Vehicle, FuelEntry, db

app = create_app()

with app.app_context():
    print("=" * 60)
    print("DETALLES DE FUELENTRY PARA 3130LDW")
    print("=" * 60)
    
    vehicle = Vehicle.query.filter_by(plate="3130LDW").first()
    if not vehicle:
        print("Vehículo no encontrado")
        sys.exit(1)
    
    print(f"\nVehículo: {vehicle.plate} (ID: {vehicle.id})")
    
    fuel_entries = FuelEntry.query.filter_by(vehicle_id=vehicle.id).order_by(FuelEntry.date.asc()).all()
    
    print(f"\nTotal FuelEntry: {len(fuel_entries)}")
    print("\nDetalles de cada entrada:")
    
    total_liters = 0
    total_subtotal = 0
    total_tax = 0
    total_amount = 0
    
    for fe in fuel_entries:
        liters = float(fe.liters or 0)
        subtotal = float(fe.subtotal_amount or 0)
        tax = float(fe.tax_amount or 0)
        amount = float(fe.total_amount or 0)
        
        total_liters += liters
        total_subtotal += subtotal
        total_tax += tax
        total_amount += amount
        
        print(f"\n  Fecha: {fe.date}")
        print(f"    Litros: {liters:.2f} L")
        print(f"    Subtotal (sin IVA): {subtotal:.2f} €")
        print(f"    IVA: {tax:.2f} €")
        print(f"    Total (con IVA): {amount:.2f} €")
        print(f"    Precio por litro: {float(fe.price_per_liter or 0):.4f} €/L")
    
    print("\n" + "-" * 60)
    print("TOTALES:")
    print(f"  Litros: {total_liters:.2f} L")
    print(f"  Subtotal (sin IVA): {total_subtotal:.2f} €")
    print(f"  IVA: {total_tax:.2f} €")
    print(f"  Total (con IVA): {total_amount:.2f} €")
    print("=" * 60)

