# Verificador de Documentos de Propiedad Venezuela

Herramienta de análisis de autenticidad para documentos de propiedad venezolanos.
Utiliza Claude Vision (Anthropic) para leer PDFs escaneados y digitales.

## Estructura del proyecto

```
verificador-propiedad-ve/
├── backend/
│   ├── main.py              # API FastAPI
│   ├── requirements.txt     # Dependencias Python
│   └── static/
│       └── index.html       # Frontend web
├── render.yaml              # Configuración de despliegue en Render
└── README.md
```

## Despliegue en Render.com (gratis)

### Paso 1 — Subir a GitHub

1. Crea un repositorio nuevo en https://github.com/new
   - Nombre sugerido: `verificador-propiedad-ve`
   - Visibilidad: Privado (recomendado)
2. Sube todos los archivos al repositorio

### Paso 2 — Crear cuenta en Render

1. Ve a https://render.com y crea una cuenta gratuita
2. Conecta tu cuenta de GitHub cuando te lo pida

### Paso 3 — Crear el servicio web

1. En el dashboard de Render, haz clic en **New → Web Service**
2. Conecta tu repositorio `verificador-propiedad-ve`
3. Render detectará automáticamente el `render.yaml`
4. Confirma la configuración:
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free

### Paso 4 — Configurar la API Key

1. En la configuración del servicio en Render, ve a **Environment**
2. Añade una variable de entorno:
   - **Key**: `ANTHROPIC_API_KEY`
   - **Value**: tu API key de Anthropic (https://console.anthropic.com)
3. Guarda y el servicio se reiniciará automáticamente

### Paso 5 — Obtener tu URL

Render te asignará una URL del tipo:
`https://verificador-propiedad-ve.onrender.com`

¡Listo! Esa es tu aplicación en línea.

## Obtener API Key de Anthropic (gratis para empezar)

1. Ve a https://console.anthropic.com
2. Crea una cuenta
3. Ve a **API Keys → Create Key**
4. Copia la clave y pégala en Render como se indica arriba

## Notas importantes

- El plan gratuito de Render duerme el servicio tras 15 min de inactividad.
  La primera carga puede tardar 30-60 segundos en despertar.
- Los PDFs se procesan en el servidor pero NO se almacenan.
- Se analizan hasta 10 páginas por documento.
- El análisis tarda entre 20-45 segundos dependiendo del número de páginas.

## Marco legal de referencia

- Código Civil Venezolano
- Ley de Registro Público y del Notariado (Gaceta Oficial N° 37.333)
- Providencias Administrativas del SAREN
- Ley de Mensajes de Datos y Firmas Electrónicas
