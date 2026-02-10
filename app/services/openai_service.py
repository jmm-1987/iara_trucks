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

Devuelve ÚNICAMENTE un objeto JSON válido, sin texto adicional, con esta estructura exacta:

{
  "doc_type": "fuel_ticket" | "insurance_policy" | "itv" | "tachograph" | "workshop_invoice" | "tires_invoice" | "other",
  "vehicle_identifier_guess": "matrícula si aparece o null",
  "vendor_name": "nombre proveedor o null",
  "vendor_tax_id": "CIF/NIF o null",
  "date_issue": "YYYY-MM-DD o null",
  "date_due": "YYYY-MM-DD para vencimientos (seguro/ITV/tacógrafo) o null",
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
  "odometer_km": número entero o null,
  "notes": "texto libre o null",
  "confidence": número entre 0 y 1
}

Reglas:
- doc_type: fuel_ticket=ticket combustible, insurance_policy=póliza seguro, itv=revisión ITV, tachograph=tacógrafo, workshop_invoice=factura taller, tires_invoice=factura neumáticos
- Usa null para campos no encontrados
- Fechas en formato YYYY-MM-DD
- Importes con punto decimal (ej: 45.99)
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

    result = {
        "doc_type": data.get("doc_type", "other"),
        "vehicle_identifier_guess": data.get("vehicle_identifier_guess"),
        "vendor_name": data.get("vendor_name"),
        "vendor_tax_id": data.get("vendor_tax_id"),
        "date_issue": data.get("date_issue"),
        "date_due": data.get("date_due"),
        "amounts": data.get("amounts") or {},
        "fuel": data.get("fuel") or {},
        "odometer_km": data.get("odometer_km"),
        "notes": data.get("notes"),
        "confidence": float(data.get("confidence", 0)),
    }

    return result
