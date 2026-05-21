"""
Servicio OpenAI - Análisis de imágenes de documentos con visión.
Extrae datos estructurados en formato JSON.
"""
import base64
import json
import logging
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)

# Prompt que fuerza salida JSON estricta
EXTRACTION_PROMPT = """Analiza esta imagen de un documento (en español) y extrae los datos relevantes.

IMPORTANTE: Identifica primero el TIPO de documento antes de extraer datos:
- fuel_ticket: Ticket de combustible/gasolina/gasoil (Repsol, Cepsa, BP, etc.) - busca litros, precio por litro, estación
- invoice: Factura comercial (tiene número de factura, IVA desglosado, datos fiscales completos)
- delivery_note: Albarán de entrega (no tiene IVA o es simplificado, suele ser entrega de mercancía)
- insurance_policy: Póliza de seguro (tiene fechas de vigencia, compañía aseguradora)
- itv: Revisión ITV (inspección técnica de vehículos)
- tachograph: Tacógrafo (documento de control de tiempos de conducción)
- workshop_invoice: Factura de taller mecánico (reparaciones, mantenimiento)
- tires_invoice: Factura de neumáticos
- other: Cualquier otro documento

En los tickets de combustible es MUY IMPORTANTE que intentes leer el valor de kilómetros del cuentakilómetros del vehículo.
Normalmente está escrito a bolígrafo en la parte superior del ticket (por ejemplo "km 123456" o "123.456 km").
Si detectas un número que parezca el cuentakilómetros, rellena el campo "kilometers" con ese valor como número entero.
Solo deja "kilometers" en null si realmente no ves ningún valor claro de cuentakilómetros.

Devuelve ÚNICAMENTE un objeto JSON válido, sin texto adicional, con esta estructura exacta:

{
  "doc_type": "fuel_ticket" | "invoice" | "delivery_note" | "insurance_policy" | "itv" | "tachograph" | "workshop_invoice" | "tires_invoice" | "other",
  "vehicle_identifier_guess": "matrícula si aparece (sin guiones ni espacios, ej: 3130LDW) o null",
  "vendor_name": "nombre proveedor o null",
  "vendor_tax_id": "CIF/NIF o null",
  "date_issue": "YYYY-MM-DD - fecha emisión según tipo (ver reglas). OBLIGATORIO para fuel_ticket.",
  "date_due": "YYYY-MM-DD - vencimiento según tipo (ver reglas). null si no aparece en el documento.",
  "invoice_date": "YYYY-MM-DD - SOLO facturas/albaranes: fecha de la factura o emisión. null en otros tipos.",
  "payment_due_date": "YYYY-MM-DD - SOLO facturas comerciales: fecha de vencimiento de PAGO (Vto.). null si no hay o en otros tipos.",
  "policy_period": {
    "valid_from": "YYYY-MM-DD inicio cobertura (desde, efecto, inicio vigencia) o null",
    "valid_to": "YYYY-MM-DD fin cobertura (hasta, fin vigencia, vencimiento póliza) o null"
  },
  "amounts": {
    "subtotal": número o null,
    "tax": número o null,
    "total": número o null,
    "currency": "EUR"
  },
  "fuel": {
    "liters": número o null,
    "price_per_liter": número o null,
    "fuel_type": "gasoil/gasolina/diesel etc o null"
  },
  "maintenance_concept": "resumen corto del concepto real facturado (ej. cambio aceite + filtros) o null",
  "kilometers": número entero (cuentakilómetros del vehículo) o null,
  "notes": "texto libre o null",
  "confidence": número entre 0 y 1
}

Reglas de detección de tipo:
- fuel_ticket: Si tiene estación de servicio, litros, precio por litro, tipo de combustible. IMPORTANTE: extrae SIEMPRE la fecha del ticket (suele estar junto a la hora, ej. 27-01-2026 21:05) y los kilómetros del cuentakilómetros si aparecen (normalmente escritos a mano arriba del ticket).
- invoice: Si tiene número de factura, IVA desglosado, datos fiscales completos (CIF, razón social)
- delivery_note: Si dice "albarán", "entrega", "delivery note", o es documento de entrega sin factura completa
- insurance_policy: Póliza/recibo de seguro. SIEMPRE hay periodo DESDE-HASTA (vigencia). valid_to/hasta = vencimiento.
- itv: Si menciona ITV, inspección técnica, ITV
- tachograph: Si menciona tacógrafo, tiempos de conducción
- workshop_invoice: Si es factura de taller, reparación, mantenimiento vehículo
- tires_invoice: Si es factura específica de neumáticos

Reglas CRÍTICAS para workshop_invoice, invoice y tires_invoice (facturas comerciales):
- Hay DOS fechas distintas si aparecen en el documento:
  1) Fecha de FACTURA / emisión → invoice_date y date_issue (ej. "Fecha", "Fecha factura", "Fecha emisión").
  2) Fecha de VENCIMIENTO de pago → payment_due_date y date_due (ej. "Vencimiento", "Fecha vencimiento", "Vto.", "Vto. pago").
- La fecha de vencimiento de pago NUNCA es la fecha de emisión de la factura.
- NUNCA pongas el vencimiento de pago en invoice_date ni en date_issue.
- Si solo ves una fecha junto a "vencimiento"/"vto" → solo payment_due_date y date_due; invoice_date y date_issue en null.
- Si solo ves la fecha de factura en cabecera → solo invoice_date y date_issue; payment_due_date y date_due en null si no hay vencimiento.
- El vencimiento de pago suele ser IGUAL o POSTERIOR a la fecha de factura (nunca antes).

Reglas CRÍTICAS para insurance_policy (seguros):
- Toda póliza/recibo tiene vigencia con fecha INICIO (desde/efecto/inicio) y fecha FIN (hasta/fin de vigencia/vencimiento).
- policy_period.valid_to y date_due deben ser la fecha FIN de cobertura (la del "HASTA", "hasta el", "vigente hasta", "vencimiento").
- policy_period.valid_from y date_issue (si no hay otra emisión) = fecha INICIO (desde, "a partir del", "efecto").
- NUNCA uses como date_due ni valid_to: "fecha de valor", "fecha valor", "value date", "fecha efecto" si es solo el inicio.
- Si aparecen varias fechas de vigencia, la MÁS TARDÍA del tramo es el vencimiento (valid_to / date_due).
- La "fecha de valor" es intermedia y debe ignorarse para vencimiento.
- En seguros NO confundas estas reglas con las de facturas de taller.

Reglas generales:
- Usa null para campos no encontrados
- Fechas: extrae la fecha del documento y devuélvela en YYYY-MM-DD (si viene con hora "27-01-2026 21:05", usa solo "2026-01-27")
- Importes con punto decimal (ej: 45.99)
- maintenance_concept: para invoice/workshop_invoice/tires_invoice, resume en 3-8 palabras el concepto REAL de la factura (p. ej. "cambio aceite motor", "sustitución neumáticos eje delantero", "revisión frenos"), sin inventar.
- Si el documento está en español, respeta los formatos locales pero normaliza en el JSON
"""


def analyze_document_image(
    image_bytes: bytes, mime_type: str = "image/jpeg", api_key: str | None = None
) -> dict[str, Any]:
    """
    Analiza una imagen de documento y extrae datos estructurados.

    Args:
        image_bytes: Contenido binario de la imagen
        mime_type: Tipo MIME (image/jpeg, image/png, etc.)
        api_key: Clave API de OpenAI (opcional, puede venir de env)

    Returns:
        Diccionario con los campos extraídos

    Raises:
        ValueError: Si la API falla o no hay clave configurada
    """
    if not api_key:
        from flask import current_app

        api_key = current_app.config.get("OPENAI_API_KEY", "")

    if not api_key:
        raise ValueError("OPENAI_API_KEY no configurada")

    client = OpenAI(api_key=api_key)

    # Codificar imagen en base64 para la API de visión
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{b64}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "high"},
                        },
                    ],
                }
            ],
            max_tokens=1024,
            temperature=0,
        )
    except Exception as e:
        logger.error("Error OpenAI API: %s", str(e))
        raise ValueError(f"Error al analizar documento: {str(e)}")

    text = response.choices[0].message.content.strip()

    # Limpiar posibles marcadores de código
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(
            line for line in lines if not line.startswith("```") and line != "json"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("JSON inválido de OpenAI: %s", text[:200])
        raise ValueError(f"No se pudo parsear la respuesta: {str(e)}")

    return _normalize_response(data)


def _normalize_response(data: dict) -> dict:
    """Normaliza la estructura de respuesta para consistencia."""
    if not isinstance(data, dict):
        return {"doc_type": "other", "confidence": 0}

    doc_type = (data.get("doc_type") or "other").lower()
    commercial_types = {
        "workshop_invoice",
        "invoice",
        "tires_invoice",
        "delivery_note",
    }
    if doc_type in commercial_types:
        date_issue = data.get("invoice_date") or data.get("date_issue")
        date_due = data.get("payment_due_date") or data.get("date_due")
    else:
        date_issue = data.get("date_issue")
        date_due = data.get("date_due")

    result = {
        "doc_type": data.get("doc_type", "other"),
        "vehicle_identifier_guess": data.get("vehicle_identifier_guess"),
        "vendor_name": data.get("vendor_name"),
        "vendor_tax_id": data.get("vendor_tax_id"),
        "date_issue": date_issue,
        "date_due": date_due,
        "invoice_date": data.get("invoice_date"),
        "payment_due_date": data.get("payment_due_date"),
        "policy_period": data.get("policy_period") or {},
        "amounts": data.get("amounts") or {},
        "fuel": data.get("fuel") or {},
        "maintenance_concept": data.get("maintenance_concept"),
        "kilometers": data.get("kilometers") or data.get("odometer_km"),
        "notes": data.get("notes"),
        "confidence": float(data.get("confidence", 0)),
    }

    return result
