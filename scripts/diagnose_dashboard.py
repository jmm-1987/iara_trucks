"""
Script de diagnóstico para verificar por qué no se muestran datos en el dashboard.
"""
import sys
from pathlib import Path
from datetime import date

# Agregar el directorio raíz al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.models import Vehicle, FuelEntry, ExpenseEntry, Document, db

app = create_app()

with app.app_context():
    print("=" * 60)
    print("DIAGNOSTICO DEL DASHBOARD")
    print("=" * 60)
    
    today = date.today()
    month_start = today.replace(day=1)
    
    print(f"\nFecha actual: {today}")
    print(f"Inicio del mes: {month_start}")
    
    # Verificar vehículos
    vehicles = Vehicle.query.filter(Vehicle.active == True).all()
    print(f"\nVehículos activos: {len(vehicles)}")
    
    for vehicle in vehicles:
        print(f"\n  Vehículo: {vehicle.plate} (ID: {vehicle.id})")
        
        # Verificar documentos
        docs = Document.query.filter(Document.vehicle_id == vehicle.id).all()
        print(f"    Documentos totales: {len(docs)}")
        
        docs_this_month = [d for d in docs if d.issue_date and d.issue_date >= month_start]
        print(f"    Documentos este mes: {len(docs_this_month)}")
        
        if docs_this_month:
            for doc in docs_this_month:
                print(f"      - {doc.doc_type} ({doc.issue_date}): {doc.status}")
        
        # Verificar FuelEntry
        fuel_entries = FuelEntry.query.filter(FuelEntry.vehicle_id == vehicle.id).all()
        print(f"    FuelEntry totales: {len(fuel_entries)}")
        
        fuel_this_month = [f for f in fuel_entries if f.date >= month_start]
        print(f"    FuelEntry este mes: {len(fuel_this_month)}")
        
        if fuel_this_month:
            total_liters = sum(float(f.liters or 0) for f in fuel_this_month)
            total_amount = sum(float(f.total_amount or 0) for f in fuel_this_month)
            print(f"      Total litros: {total_liters:.1f} L")
            print(f"      Total importe: {total_amount:.2f} €")
            for f in fuel_this_month:
                print(f"      - {f.date}: {f.liters} L, {f.total_amount} €")
        else:
            # Mostrar todos los FuelEntry para ver las fechas
            if fuel_entries:
                print(f"    FuelEntry anteriores:")
                for f in fuel_entries[:5]:  # Mostrar solo los primeros 5
                    print(f"      - {f.date}: {f.liters} L, {f.total_amount} €")
        
        # Verificar ExpenseEntry
        expense_entries = ExpenseEntry.query.filter(ExpenseEntry.vehicle_id == vehicle.id).all()
        print(f"    ExpenseEntry totales: {len(expense_entries)}")
        
        expense_this_month = [e for e in expense_entries if e.date >= month_start]
        print(f"    ExpenseEntry este mes: {len(expense_this_month)}")
        
        if expense_this_month:
            total_expense = sum(float(e.total_amount or 0) for e in expense_this_month)
            print(f"      Total gastos: {total_expense:.2f} €")
            for e in expense_this_month:
                print(f"      - {e.date}: {e.category}, {e.total_amount} €")
        else:
            # Mostrar todos los ExpenseEntry para ver las fechas
            if expense_entries:
                print(f"    ExpenseEntry anteriores:")
                for e in expense_entries[:5]:  # Mostrar solo los primeros 5
                    print(f"      - {e.date}: {e.category}, {e.total_amount} €")
    
    print("\n" + "=" * 60)
    print("FIN DEL DIAGNOSTICO")
    print("=" * 60)

