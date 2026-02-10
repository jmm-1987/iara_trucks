# Gestión de Flotas

Sistema de gestión de flotas con integración de **Telegram Bot** y **OpenAI Vision** para extracción de datos de documentos (pólizas, tickets de combustible, ITV, tacógrafo, facturas de taller, neumáticos, etc.).

## Requisitos

- Python 3.11+
- Cuenta OpenAI con API key
- Bot de Telegram (crear con [@BotFather](https://t.me/BotFather))

## Instalación

```bash
# Clonar o entrar en el directorio del proyecto
cd prueba

# Crear entorno virtual
python -m venv venv

# Activar (Windows)
venv\Scripts\activate

# Activar (Linux/Mac)
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Copiar variables de entorno
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/Mac

# Editar .env con tus claves
```

## Variables de entorno (.env)

| Variable | Descripción |
|----------|-------------|
| `FLASK_SECRET_KEY` | Clave secreta para sesiones (genera una aleatoria) |
| `OPENAI_API_KEY` | Clave API de OpenAI (sk-...) |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram |
| `DATABASE_URL` | URL de SQLite (por defecto: sqlite:///fleet_management.db) |
| `MAX_UPLOAD_SIZE_MB` | Tamaño máximo de subida en MB (default: 10) |
| `ENABLE_SCHEDULER` | 1 para activar procesamiento automático de pendientes (default: 1) |

## Inicialización de la base de datos

```bash
python scripts/init_db.py
```

## Ejecución

```bash
python run.py
```

Arranca el **panel web** (http://127.0.0.1:5000) y el **bot de Telegram** en modo polling en un solo proceso. Si no tienes `TELEGRAM_BOT_TOKEN` configurada, el bot se desactiva y solo corre la web.

**Alternativa:** Para ejecutar solo la web: `flask run`. Para ejecutar solo el bot: `python scripts/run_telegram_polling.py`.

### Webhook (producción)

Para producción, configura el webhook de Telegram:

```
POST https://tu-dominio.com/telegram/webhook
```

Con el header `X-Telegram-Bot-Api-Secret-Token` si definiste `WEBHOOK_SECRET`.

## Estructura del proyecto

```
prueba/
├── app/
│   ├── __init__.py          # Factory create_app
│   ├── config.py            # Configuración
│   ├── models.py            # Modelos SQLAlchemy
│   ├── services/
│   │   ├── openai_service.py      # Análisis de imágenes con OpenAI
│   │   ├── extraction_service.py  # Normalización de datos
│   │   ├── telegram_service.py    # API Telegram
│   │   ├── document_processor.py  # Procesamiento de documentos
│   │   ├── reporting_service.py   # Reportes y exportación
│   │   ├── reminders_service.py   # Recordatorios
│   │   └── scheduler_service.py   # Jobs en background
│   ├── routes/
│   │   ├── web.py                 # Rutas del panel
│   │   └── telegram_webhook.py    # Webhook/polling Telegram
│   ├── templates/
│   └── static/
├── scripts/
│   ├── init_db.py
│   └── run_telegram_polling.py
├── tests/
├── uploads/                 # Archivos subidos (gitignore)
├── .env.example
├── requirements.txt
└── README.md
```

## Uso del bot de Telegram

1. **/start** – Mensaje de bienvenida y ayuda
2. **/vehiculo** – Lista vehículos y permite seleccionar o escribir matrícula
3. **Enviar imagen** – Procesa el documento con OpenAI, extrae datos y guarda en la base de datos

Tipos de documentos soportados:
- Ticket de combustible (fuel_ticket)
- Póliza de seguro (insurance_policy)
- ITV (itv)
- Tacógrafo (tachograph)
- Factura de taller (workshop_invoice)
- Factura de neumáticos (tires_invoice)

## Panel web

- **Dashboard**: KPIs, próximos vencimientos
- **Vehículos**: CRUD
- **Documentos**: Listado, filtros, detalle, reprocesar
- **Reportes**: Consumo combustible, gastos por categoría, vencimientos, exportación CSV
- **Vencimientos**: Lista de seguros, ITV, tacógrafo

## Tests

```bash
pytest
pytest -v
pytest --cov=app
```

## Notas de desarrollo

- Si OpenAI falla, el documento se guarda como `error` y se puede reprocesar desde el panel
- El scheduler procesa documentos pendientes cada 5 minutos (desactivable con `ENABLE_SCHEDULER=0`)
- Los PDF se aceptan en la subida pero el procesamiento con visión requiere imágenes; convierte PDF a imagen si es necesario
