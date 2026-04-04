import os
import base64
import json
import logging
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import fitz  # PyMuPDF

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Verificador de Documentos de Propiedad Venezuela")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = "claude-sonnet-4-5"
MAX_PAGES = 10
RENDER_SCALE = 2.0

# ── Error handlers ─────────────────────────────────────────────────────────────
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    log.error("HTTP %s — %s", exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": True, "codigo": exc.status_code, "detalle": str(exc.detail)},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.error("Validation error: %s", exc.errors())
    return JSONResponse(
        status_code=422,
        content={"error": True, "codigo": 422, "detalle": "Solicitud inválida", "errores": exc.errors()},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.exception("Error no controlado: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": True, "codigo": 500, "detalle": f"Error interno: {type(exc).__name__}: {str(exc)}"},
    )

# ── System prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Eres un experto en derecho registral venezolano y análisis forense de documentos de propiedad inmobiliaria.
Conoces el Código Civil venezolano, la Ley de Registro Público y del Notariado, y todos los procedimientos del SAREN.

Se te proporcionan imágenes de las páginas de un documento PDF de propiedad venezolano.
Lee visualmente el contenido completo: texto impreso, manuscrito, sellos, firmas y notas marginales.

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional, sin markdown, sin backticks.

Estructura exacta:
{
  "tipo_documento": "string (ej: Compraventa, Hipoteca, Título Supletorio, Poder, etc.)",
  "score": número entre 0 y 100,
  "veredicto": "AUTÉNTICO" | "SOSPECHOSO" | "FALSIFICADO",
  "campos_encontrados": {
    "numero_documento": "valor o null",
    "matricula_inmueble": "valor o null",
    "registro_publico": "valor o null",
    "fecha_protocolizacion": "valor o null",
    "registrador": "valor o null",
    "abogado_inpreabogado": "valor o null",
    "codigo_catastral": "valor o null",
    "partes_identificadas": "resumen de las partes con cédulas",
    "inmueble_descripcion": "descripción breve del inmueble",
    "pub_saren": "número PUB o null",
    "numero_tramite_saren": "número de trámite o null",
    "precio": "valor o null",
    "sellos_presentes": true,
    "firmas_presentes": true,
    "linderos_presentes": true,
    "nota_registrador": true
  },
  "checks": [
    {
      "nombre": "string",
      "resultado": "OK" | "WARN" | "FAIL" | "INFO",
      "detalle": "string explicativo"
    }
  ],
  "alertas": ["lista de alertas o irregularidades detectadas"],
  "resumen": "explicación del veredicto en 2-3 oraciones"
}

Checks obligatorios:
1. Identificación de partes con cédulas de identidad
2. Número de documento y matrícula del inmueble
3. Nombre, firma y cédula del Registrador Público
4. Número PUB (código de barras SAREN)
5. Número de trámite SAREN
6. Fecha de protocolización (verificar que sea día hábil, lunes-viernes)
7. Abogado redactor con número de Inpreabogado
8. Descripción del inmueble con linderos cardinales (Norte, Sur, Este, Oeste)
9. Código catastral municipal
10. Sellos húmedos visibles
11. Coherencia entre todas las fechas del documento
12. Leyes venezolanas citadas correctamente
13. Nota marginal del registrador
14. Cadena de titularidad (documento anterior)
15. Consistencia tipográfica y de formato

Criterios de puntuación:
- 85-100: AUTÉNTICO — todos los elementos clave presentes y coherentes
- 50-84: SOSPECHOSO — elementos faltantes o inconsistencias menores
- 0-49: FALSIFICADO — elementos críticos ausentes o inconsistencias graves

IMPORTANTE: Si no puedes leer bien un campo por calidad de imagen, márcalo como WARN.
No marques FAIL por baja resolución; solo por ausencia confirmada o inconsistencia real."""


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    key = ANTHROPIC_API_KEY
    key_ok = bool(key and key.startswith("sk-ant-") and len(key) > 20)
    return {
        "status": "ok" if key_ok else "degradado",
        "modelo": MODEL,
        "api_key_presente": bool(key),
        "api_key_valida": key_ok,
        "api_key_prefijo": key[:10] + "..." if key else "—",
        "max_paginas": MAX_PAGES,
    }


@app.post("/analizar")
async def analizar_documento(file: UploadFile = File(...)):

    # ── 1. Validar API key ────────────────────────────────────────────────────
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY no configurada")
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY no está configurada. Ve a Render → Environment y añade la variable.",
        )
    if not ANTHROPIC_API_KEY.startswith("sk-ant-"):
        log.error("ANTHROPIC_API_KEY con formato incorrecto: %s...", ANTHROPIC_API_KEY[:10])
        raise HTTPException(
            status_code=500,
            detail=f"ANTHROPIC_API_KEY parece incorrecta (prefijo: {ANTHROPIC_API_KEY[:10]}). Debe empezar con 'sk-ant-'.",
        )

    # ── 2. Validar archivo ────────────────────────────────────────────────────
    filename = file.filename or "documento.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) == 0:
        raise HTTPException(status_code=400, detail="El archivo PDF está vacío.")
    if len(pdf_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El archivo supera el límite de 50 MB.")

    log.info("Recibido: %s (%.1f KB)", filename, len(pdf_bytes) / 1024)

    # ── 3. Convertir PDF a imágenes ───────────────────────────────────────────
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        log.exception("Error abriendo PDF")
        raise HTTPException(status_code=400, detail=f"No se pudo abrir el PDF: {e}")

    total_pages = min(doc.page_count, MAX_PAGES)
    log.info("Procesando %d página(s) de %s", total_pages, filename)

    page_images_b64 = []
    try:
        for i in range(total_pages):
            page = doc[i]
            mat = fitz.Matrix(RENDER_SCALE, RENDER_SCALE)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("jpeg")
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            page_images_b64.append(b64)
            log.info("Página %d/%d renderizada (%d bytes)", i + 1, total_pages, len(img_bytes))
    except Exception as e:
        log.exception("Error renderizando páginas")
        raise HTTPException(status_code=500, detail=f"Error procesando páginas del PDF: {e}")
    finally:
        doc.close()

    # ── 4. Construir payload para Claude ──────────────────────────────────────
    content_blocks = [
        {
            "type": "text",
            "text": (
                f"Analiza este documento venezolano de propiedad.\n"
                f"Archivo: {filename}\n"
                f"Páginas analizadas: {total_pages}\n"
                "Lee todas las páginas cuidadosamente antes de responder."
            ),
        }
    ]
    for i, b64 in enumerate(page_images_b64):
        content_blocks.append({"type": "text", "text": f"--- Página {i + 1} de {total_pages} ---"})
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    # ── 5. Llamar a la API de Anthropic ──────────────────────────────────────
    log.info("Enviando a Claude Vision (%s)...", MODEL)
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            api_response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 2000,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": content_blocks}],
                },
            )
    except httpx.TimeoutException:
        log.error("Timeout al llamar a Anthropic")
        raise HTTPException(
            status_code=504,
            detail="Tiempo de espera agotado (>120s). El documento puede tener demasiadas páginas.",
        )
    except httpx.ConnectError as e:
        log.error("Error de conexión con Anthropic: %s", e)
        raise HTTPException(status_code=502, detail=f"No se pudo conectar con Anthropic: {e}")
    except Exception as e:
        log.exception("Error inesperado llamando a Anthropic")
        raise HTTPException(status_code=500, detail=f"Error inesperado: {type(e).__name__}: {e}")

    # ── 6. Validar respuesta de Anthropic ─────────────────────────────────────
    log.info("Respuesta Anthropic: HTTP %d", api_response.status_code)

    if api_response.status_code == 401:
        raise HTTPException(
            status_code=500,
            detail="API key rechazada por Anthropic (401). Verifica la clave en Render → Environment.",
        )
    if api_response.status_code == 429:
        raise HTTPException(
            status_code=429,
            detail="Límite de uso de la API alcanzado (429). Espera unos minutos e intenta de nuevo.",
        )
    if api_response.status_code != 200:
        body = api_response.text[:600]
        log.error("Anthropic error %d: %s", api_response.status_code, body)
        raise HTTPException(
            status_code=500,
            detail=f"Error de Anthropic ({api_response.status_code}): {body}",
        )

    # ── 7. Parsear JSON del modelo ────────────────────────────────────────────
    try:
        data = api_response.json()
        raw_text = "".join(block.get("text", "") for block in data.get("content", []))
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_text)
        log.info("Análisis completado — veredicto: %s, score: %s", result.get("veredicto"), result.get("score"))
        return result
    except json.JSONDecodeError as e:
        log.error("JSON inválido: %s | texto: %s", e, clean_text[:300])
        raise HTTPException(
            status_code=500,
            detail=f"El modelo devolvió una respuesta no parseable: {clean_text[:300]}",
        )
    except Exception as e:
        log.exception("Error procesando respuesta")
        raise HTTPException(status_code=500, detail=f"Error procesando respuesta: {e}")
