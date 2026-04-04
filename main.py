import os
import base64
import json
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import fitz  # PyMuPDF

app = FastAPI(title="Verificador de Documentos de Propiedad Venezuela")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
  "alertas": ["lista de alertas o irregularidades"],
  "resumen": "explicación del veredicto en 2-3 oraciones"
}

Checks obligatorios a incluir:
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

IMPORTANTE: Si no puedes leer bien un campo por calidad de imagen, márcalo como WARN con detalle explicativo.
No marques como FAIL por baja resolución; solo por ausencia confirmada o inconsistencia real."""


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/analizar")
async def analizar_documento(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")

    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="API key de Anthropic no configurada")

    pdf_bytes = await file.read()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo abrir el PDF: {str(e)}")

    total_pages = min(doc.page_count, 10)
    page_images_b64 = []

    for i in range(total_pages):
        page = doc[i]
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("jpeg")
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        page_images_b64.append(b64)

    doc.close()

    content_blocks = [
        {
            "type": "text",
            "text": f"Analiza este documento venezolano de propiedad. Archivo: {file.filename}. Páginas analizadas: {total_pages}. Lee todas las páginas con atención."
        }
    ]

    for i, b64 in enumerate(page_images_b64):
        content_blocks.append({"type": "text", "text": f"--- Página {i + 1} de {total_pages} ---"})
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64
            }
        })

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-opus-4-5",
                "max_tokens": 2000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": content_blocks}]
            }
        )

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Error de la API: {response.text}")

    data = response.json()
    raw_text = "".join(block.get("text", "") for block in data.get("content", []))
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()

    try:
        result = json.loads(clean_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Error procesando respuesta de IA")

    return result


@app.get("/health")
async def health():
    return {"status": "ok"}
