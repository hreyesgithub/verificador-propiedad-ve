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
import re

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Verificador de Documentos de Propiedad Venezuela")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = "claude-3-5-sonnet" # "claude-sonnet-4-5"
MAX_TOTAL_KB = 3500   # límite seguro (~3.5MB)
RENDER_SCALE = 1.5    # Reducido de 2.0 a 1.5 para bajar tamaño de payload
MAX_PAGES = 20        # Aumentado a 20 páginas


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    log.error("HTTP %s — %s", exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": True, "codigo": exc.status_code, "detalle": str(exc.detail)},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"error": True, "codigo": 422, "detalle": "Solicitud inválida", "errores": exc.errors()},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.exception("Error no controlado: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": True, "codigo": 500, "detalle": f"{type(exc).__name__}: {str(exc)}"},
    )


SYSTEM_PROMPT = """Eres un experto en derecho registral venezolano y análisis forense de documentos de propiedad inmobiliaria.
Conoces el Código Civil venezolano, la Ley de Registro Público y del Notariado, y todos los procedimientos del SAREN.

Se te proporcionan imágenes de las páginas de un documento PDF de propiedad venezolano.
Lee visualmente el contenido completo: texto impreso, manuscrito, sellos, firmas y notas marginales.

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional, sin markdown, sin backticks.

Estructura exacta:
{
  "tipo_documento": "string",
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
    {"nombre": "string", "resultado": "OK" | "WARN" | "FAIL" | "INFO", "detalle": "string"}
  ],
  "alertas": ["lista de alertas"],
  "resumen": "explicación del veredicto en 2-3 oraciones"
}

Checks obligatorios: identificación de partes con cédulas, número de documento y matrícula, registrador con cédula, PUB SAREN, número trámite SAREN, fecha protocolización (día hábil), abogado con Inpreabogado, linderos cardinales, código catastral, sellos húmedos, coherencia de fechas, leyes venezolanas citadas, nota marginal registrador, cadena de titularidad, consistencia tipográfica.

Puntuación: 85-100 AUTÉNTICO, 50-84 SOSPECHOSO, 0-49 FALSIFICADO.
Si imagen de baja calidad: WARN, no FAIL. Solo FAIL por ausencia confirmada o inconsistencia real."""


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    key = bool(ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-"))
    key_ok = bool(key and key.startswith("sk-ant-") and len(key) > 20)
    modelo_ok = await validar_modelo() if key_ok else False
    
    return {
        "status": "ok" if key_ok and modelo_ok else "degradado",
        "modelo": MODEL,
        "modelo_valido": modelo_ok,
        "api_key_presente": bool(key),
        "api_key_valida": key_ok,
        "api_key_prefijo": key[:10] + "..." if key else "—",
        "max_paginas": MAX_PAGES,
        "render_scale": RENDER_SCALE,
    }


def pdf_to_images(pdf_bytes: bytes) -> tuple[list[str], int]:
    """Convierte PDF a lista de imágenes base64. Devuelve (imagenes, total_paginas)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = min(doc.page_count, MAX_PAGES)
    images = []
    total_kb = 0
    try:
        for i in range(total_pages):
            page = doc[i]
            mat = fitz.Matrix(RENDER_SCALE, RENDER_SCALE)
            pix = page.get_pixmap(matrix=mat)
            
            img_bytes = pix.tobytes("jpeg")
            kb = len(img_bytes) // 1024
            # 🔴 CORTE INTELIGENTE
            if total_kb + kb > MAX_TOTAL_KB:
                log.warning("Límite de payload alcanzado en página %d", i + 1)
                break

            total_kb += kb
                
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            images.append(b64)
            log.info("Página %d/%d — %d KB", i + 1, total_pages, len(img_bytes) // 1024)
    finally:
        doc.close()
    log.info("Payload final: %d KB en %d páginas", total_kb, len(images))
    return images, total_pages


@app.post("/analizar")
async def analizar_documento(file: UploadFile = File(...)):

    # 1. Validar API key
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY no configurada en Render → Environment.")
    if not ANTHROPIC_API_KEY.startswith("sk-ant-"):
        raise HTTPException(status_code=500, detail=f"API key con formato incorrecto (prefijo: {ANTHROPIC_API_KEY[:10]})")

    # 2. Validar archivo
    filename = file.filename or "documento.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="El archivo PDF está vacío.")
    if len(pdf_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El archivo supera 50 MB.")

    log.info("Archivo recibido: %s (%.1f KB)", filename, len(pdf_bytes) / 1024)

    # 3. Convertir PDF a imágenes
    try:
        images, total_pages = pdf_to_images(pdf_bytes)
    except Exception as e:
        log.exception("Error convirtiendo PDF")
        raise HTTPException(status_code=400, detail=f"No se pudo procesar el PDF: {e}")

    log.info("Total páginas a analizar: %d", total_pages)

    # 4. Construir mensaje para Claude
    content_blocks = [{
        "type": "text",
        "text": f"Analiza este documento venezolano de propiedad.\nArchivo: {filename}\nPáginas: {total_pages}\nLee todas las páginas antes de responder."
    }]
    for i, b64 in enumerate(images):
        content_blocks.append({"type": "text", "text": f"--- Página {i + 1} de {total_pages} ---"})
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })

    total_img_kb = sum(len(b) * 3 // 4 // 1024 for b in images)
    log.info("Payload imágenes: ~%d KB en %d páginas", total_img_kb, total_pages)

    # 5. Llamar a Anthropic
    log.info("Llamando a %s...", MODEL)
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            api_response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 1200, #2000,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": content_blocks}],
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout (>180s). Documento demasiado grande.")
    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"No se pudo conectar con Anthropic: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    log.info("Anthropic respondió HTTP %d", api_response.status_code)

    # 6. Manejar errores de Anthropic con detalle
    #if api_response.status_code != 200:
    #    body = api_response.text
    #    log.error("Anthropic error: %s", body)
    #        raise HTTPException(
    #            status_code=500,
    #            detail=f"Anthropic error {api_response.status_code}: {body}"
    #        )

    #    msgs = {
    #        401: "API key rechazada. Verifica la clave en Render → Environment.",
    #        403: "Acceso denegado. La API key no tiene permisos suficientes.",
    #        429: "Límite de uso alcanzado. Espera unos minutos e intenta de nuevo.",
    #        413: "Payload demasiado grande. El documento tiene demasiadas páginas o resolución muy alta.",
    #        500: f"Error interno de Anthropic: {body[:400]}",
    #        529: "Anthropic está sobrecargado. Intenta de nuevo en unos minutos.",
    #    }
    #    detail = msgs.get(api_response.status_code, f"Error {api_response.status_code}: {body[:400]}")
    #    raise HTTPException(status_code=500, detail=detail)

    if api_response.status_code != 200:
        try:
            error_json = api_response.json()
        except:
            error_json = api_response.text
    
        log.error("Anthropic error: %s", error_json)
    
        raise HTTPException(
            status_code=500,
            detail={
                "mensaje": "Error en Anthropic",
                "codigo": api_response.status_code,
                "detalle": error_json,
            },
        )

    # 7. Parsear respuesta
    try:
        data = api_response.json()
        if "content" not in data:
            raise HTTPException(status_code=500, detail="Respuesta inválida de Anthropic")
            
        # raw_text = "".join(block.get("text", "") for block in data.get("content", []))
        raw_text = data["content"][0]["text"]

        # Extraer JSON limpio
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)

        if not match:
            raise HTTPException(status_code=500, detail="No se pudo extraer JSON del modelo")

        json_str = match.group(0)

        try:
            result = json.loads(json_str)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"JSON inválido: {e}")
        
        #clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        #result = json.loads(clean_text)
        log.info("Veredicto: %s — Score: %s", result.get("veredicto"), result.get("score"))
        return result
    except json.JSONDecodeError:
        log.error("JSON inválido del modelo: %s", clean_text[:400])
        raise HTTPException(status_code=500, detail=f"Respuesta no parseable del modelo: {clean_text[:300]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando respuesta: {e}")

async def validar_modelo():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        return r.status_code == 200
    except:
        return False
