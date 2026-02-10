#!/usr/bin/env python3
"""
Punto de entrada único: arranca web + bot Telegram.
Uso: python run.py
"""
import os
import threading

from dotenv import load_dotenv

load_dotenv()


def run_telegram_polling(app):
    """Ejecuta el bot de Telegram en modo polling (en segundo plano)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("⚠️ TELEGRAM_BOT_TOKEN no configurada. Bot desactivado.")
        return

    import requests

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    offset = 0

    print("🤖 Bot Telegram en modo polling (activo)")
    while True:
        try:
            with app.app_context():
                r = requests.get(url, params={"offset": offset, "timeout": 30})
                data = r.json()
                if not data.get("ok"):
                    continue
                for u in data.get("result", []):
                    offset = u["update_id"] + 1
                    from app.routes.telegram_webhook import process_update

                    process_update(u, token)
        except Exception as e:
            import logging

            logging.getLogger(__name__).exception("Error polling: %s", e)


if __name__ == "__main__":
    from app import create_app

    app = create_app()

    # Bot Telegram en segundo plano
    t = threading.Thread(target=run_telegram_polling, args=(app,), daemon=True)
    t.start()

    # Web
    print("🌐 Web en http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
