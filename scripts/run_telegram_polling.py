#!/usr/bin/env python3
"""
Script para ejecutar el bot de Telegram en modo polling.
Uso: python scripts/run_telegram_polling.py

Requiere: TELEGRAM_BOT_TOKEN en .env
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

def run_polling():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN no configurada en .env")
        sys.exit(1)

    from app import create_app

    app = create_app()

    import requests

    # Obtener updates pendientes y procesarlos
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    offset = 0

    print("Bot en modo polling. Presiona Ctrl+C para salir.")
    while True:
        try:
            with app.app_context():
                r = requests.get(url, params={"offset": offset, "timeout": 30})
                data = r.json()
                if not data.get("ok"):
                    print("Error API Telegram:", data)
                    continue
                results = data.get("result", [])
                for u in results:
                    offset = u["update_id"] + 1
                    from app.routes.telegram_webhook import process_update

                    process_update(u, token)
        except KeyboardInterrupt:
            print("\nDetenido.")
            break
        except Exception as e:
            logging.exception("Error en polling: %s", e)


if __name__ == "__main__":
    run_polling()
