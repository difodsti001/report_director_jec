"""
app.py

Endpoints del asistente de reporte institucional para el directivo.
"""

import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import core

core.configurar_logging(nivel=os.environ.get("LOG_LEVEL", "INFO"))
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("app_startup")
    await core.abrir_pools()
    yield
    log.info("app_shutdown")
    await core.cerrar_pools()


app = FastAPI(title="Reporte institucional - Hito 1 Directivo", lifespan=lifespan)

# Dev: permite que el dashboard HTML servido en otro puerto/origen llame a la API.
# Restringir allow_origins a los dominios reales antes de producción.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")


class ReporteRequest(BaseModel):
    userid: int


@app.get("/reporte_director", response_class=HTMLResponse)
async def reporte_dashboard(request: Request):
    return templates.TemplateResponse(request, "index_director.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/reporte/consultar")
async def consultar_reporte(userid: int):
    """
    Endpoint rápido de solo lectura (nunca toca el LLM). Responde con
    una acción que le indica al frontend qué hacer:

      - accion="mostrar":          reporte vigente, se devuelve completo.
      - accion="generar":          no hay reporte o está desactualizado;
                                    llamar a POST /reporte/generar.
      - accion="esperar_docentes": aún no hay suficientes docentes
                                    evaluados; no generar todavía.
    """
    try:
        return await core.consultar_reporte_existente(userid)

    except core.DirectivoNoEncontradoError:
        log.warning("endpoint_consultar_directivo_no_encontrado", userid=userid)
        raise HTTPException(404, f"No se encontró IE asociada al userid {userid}")

    except Exception:
        log.error("endpoint_consultar_error_no_manejado", userid=userid, exc_info=True)
        raise HTTPException(500, "Error interno al consultar el reporte")


@app.post("/reporte/generar")
async def generar_reporte_endpoint(payload: ReporteRequest, courseid: int, cmid: int):
    """
    Pipeline completo: resuelve IE, verifica caché con invalidación por
    n_docentes_evaluados, genera con LLM si hace falta, guarda y registra
    la asociación directivo-reporte.
    """
    try:
        return await core.obtener_o_generar_reporte(payload.userid, courseid, cmid)

    except core.DirectivoNoEncontradoError:
        log.warning("endpoint_generar_directivo_no_encontrado", userid=payload.userid)
        raise HTTPException(404, f"No se encontró IE asociada al userid {payload.userid}")

    except core.SinDatosDisponiblesError:
        log.warning("endpoint_generar_sin_datos", userid=payload.userid, courseid=courseid)
        raise HTTPException(404, "No hay evidencias de Sesión registradas todavía para esta IE")

    except Exception:
        log.error(
            "endpoint_generar_error_no_manejado",
            userid=payload.userid,
            courseid=courseid,
            exc_info=True,
        )
        raise HTTPException(500, "Error interno al generar el reporte")