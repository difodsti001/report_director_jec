"""
core.py

Lógica de negocio del asistente de reporte institucional.

Dos bases de datos, dos pools separados:
- pool_datos:  base con public."base_ebr_IA" y vw_processing_brechas_jec
- pool_cache:  base con f4_reportes_directivo y f4_consultas_directivo
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from llm_client import generar_texto_narrativo


# =============================================================================
# Logging estructurado
# =============================================================================

def configurar_logging(nivel: str = "INFO") -> None:
    """Llamar UNA VEZ al arrancar la aplicación, antes de loguear nada."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, nivel.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, nivel.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger(__name__)


# =============================================================================
# Excepciones de negocio
# =============================================================================

class DirectivoNoEncontradoError(Exception):
    """El userid no resuelve a ninguna IE en base_ebr_IA."""


class SinDatosDisponiblesError(Exception):
    """No hay filas en vw_processing_brechas_jec para ese cod_modular,
    o ningún curso de docentes tiene cmids configurados todavía."""


class CursoDocentesNoConfiguradoError(Exception):
    """El course_id de docentes existe en el diccionario pero sus cmids
    aún no se definieron (valor None)."""


class ReporteNoExisteError(Exception):
    """No hay reporte guardado todavía para esta IE. Distinto de
    SinDatosDisponiblesError: este es el caso 'aún no se generó', usado
    por el endpoint de solo-consulta."""


class MuestraInsuficienteError(Exception):
    """No usada actualmente para bloquear generación -- se mantiene
    declarada por si se requiere en el futuro. La condición de muestra
    insuficiente hoy se maneja como dato dentro del reporte
    (variables["muestra_insuficiente"]), no como bloqueo: el reporte se
    genera igual, marcado como referencial. Ver generar_reporte."""


# =============================================================================
# Zona horaria
# =============================================================================

# Perú (todo el territorio, incluida Lima) usa UTC-5 todo el año, sin
# horario de verano -- un offset fijo es correcto y no requiere una base
# de datos de zonas horarias (zoneinfo/tzdata) instalada en el servidor.
ZONA_LIMA = timezone(timedelta(hours=-5))


# =============================================================================
# Configuración de conexión
# =============================================================================

_KEEPALIVE_PARAMS = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3"


def _con_keepalives(dsn: str) -> str:
    separador = "&" if "?" in dsn else "?"
    return f"{dsn}{separador}{_KEEPALIVE_PARAMS}"


DSN_DATOS = _con_keepalives(os.environ["DSN_DATOS"])
DSN_CACHE = _con_keepalives(os.environ["DSN_CACHE"])

_MAX_IDLE = int(os.environ.get("POOL_MAX_IDLE_SECONDS", "300"))

_POOL_KWARGS = dict(
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
    max_idle=_MAX_IDLE,
    check=AsyncConnectionPool.check_connection,
)

pool_datos = AsyncConnectionPool(conninfo=DSN_DATOS, **_POOL_KWARGS)
pool_cache = AsyncConnectionPool(conninfo=DSN_CACHE, **_POOL_KWARGS)


async def abrir_pools() -> None:
    """Llamar en el evento startup de FastAPI."""
    await pool_datos.open()
    await pool_cache.open()
    await _asegurar_schema_cache()


async def cerrar_pools() -> None:
    """Llamar en el evento shutdown de FastAPI."""
    await pool_datos.close()
    await pool_cache.close()


async def _asegurar_schema_cache() -> None:
    """Crea las tablas de caché si no existen."""
    ddl = """
        CREATE TABLE IF NOT EXISTS f4_reportes_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL UNIQUE,
            estado VARCHAR NOT NULL DEFAULT 'generado',
            n_docentes_evaluados INTEGER NOT NULL,
            reporte_json JSONB,
            fecha_generacion TIMESTAMP NOT NULL DEFAULT now(),
            fecha_actualizacion TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT chk_estado CHECK (estado IN ('generado', 'muestra_insuficiente', 'sin_datos')),
            CONSTRAINT chk_reporte_json_si_generado
                CHECK (estado <> 'generado' OR reporte_json IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS f4_consultas_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL,
            userid_directivo INTEGER NOT NULL,
            courseid INTEGER NOT NULL,
            cmid INTEGER NOT NULL,
            id_reporte_directivo INTEGER NOT NULL REFERENCES f4_reportes_directivo(id),
            fecha_consulta TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT uq_cod_modular_userid UNIQUE (cod_modular, userid_directivo)
        );

        CREATE INDEX IF NOT EXISTS idx_consultas_cod_modular ON f4_consultas_directivo (cod_modular);
        CREATE INDEX IF NOT EXISTS idx_consultas_userid ON f4_consultas_directivo (userid_directivo);
        CREATE INDEX IF NOT EXISTS idx_consultas_reporte ON f4_consultas_directivo (id_reporte_directivo);
    """
    async with pool_cache.connection() as conn:
        await conn.execute(ddl)
        await conn.commit()


# =============================================================================
# Configuración de negocio
# =============================================================================
COLUMNAS_FILTRO_IE = ("nombre_ie", '"región"', "distrito")
_SEPARADOR_CLAVE_IE = "|"


def _armar_clave_ie(nombre_ie: str, region: str, distrito: str) -> str:
    """Arma la clave compuesta que identifica una IE en el caché, a
    partir de los 3 valores ya resueltos desde la consulta SQL."""
    return _SEPARADOR_CLAVE_IE.join([nombre_ie or "", region or "", distrito or ""])


# =============================================================================
# Referencia de criterios y brechas
# =============================================================================

CRITERIOS: dict[str, dict[str, str]] = {
    "C1": {
        "nombre": "Coherencia entre propósito y desafíos",
        "que_evalua": (
            "Que la sesión responda a los desafíos identificados en el "
            "diagnóstico previo (MPE/ENLA/evidencias de aula), no a un "
            "formato genérico."
        ),
    },
    "C2": {
        "nombre": "Situación significativa",
        "que_evalua": (
            "Que la situación de aprendizaje esté orientada al desarrollo "
            "de competencias y pensamiento crítico, no solo a completar "
            "actividades."
        ),
    },
    "C3": {
        "nombre": "Demanda cognitiva e involucramiento activo",
        "que_evalua": (
            "Que las actividades propuestas exijan análisis, toma de "
            "decisiones o argumentación, no solo participación."
        ),
    },
    "C4": {
        "nombre": "Mediación y evaluación formativa",
        "que_evalua": (
            "Que las estrategias de mediación y los criterios de "
            "evaluación formativa sean coherentes entre sí y con el "
            "propósito."
        ),
    },
    "C5": {
        "nombre": "Recursos y espacios",
        "que_evalua": (
            "Que los recursos y espacios educativos elegidos respondan a "
            "una decisión pedagógica, no a disponibilidad."
        ),
    },
}

# Denominación visible según la ficha técnica F4, sección 8. Los IDs
# B1-B5 se conservan solo en la capa de datos (campo "id" interno) -- el
# reporte visible al directivo nunca debe mostrar el código.
BRECHAS: dict[str, dict[str, str]] = {
    "B1": {"nombre": "Coherencia del diseño con la información del diagnóstico", "tipo": "Crítica"},
    "B2": {"nombre": "Participación de los estudiantes y nivel de exigencia de las actividades", "tipo": "Crítica"},
    "B3": {"nombre": "Situaciones significativas", "tipo": "Estructural"},
    "B4": {"nombre": "Mediación y evaluación formativa", "tipo": "Estructural"},
    "B5": {"nombre": "Uso pedagógico de recursos y espacios", "tipo": "De ajuste"},
}


def _nombre_criterio(criterio_id: str) -> str:
    """'C1' -> 'Coherencia entre propósito y desafíos'. Nunca antepone el
    código: el documento prohíbe mostrar C1-C5/B1-B5 en texto visible al
    directivo. Si el id no está en el diccionario (por ejemplo un criterio
    nuevo aún no documentado), retorna el id tal cual, sin romper el flujo."""
    info = CRITERIOS.get(criterio_id)
    return info["nombre"] if info else criterio_id


def _nombre_brecha(brecha_id: str) -> str:
    """'B4' -> 'Mediación y evaluación formativa' (denominación visible,
    sin el código -- ver _nombre_criterio)."""
    info = BRECHAS.get(brecha_id)
    return info["nombre"] if info else brecha_id

CARGOS_DOCENTE = ("DOCENTE",)

# Prioridad de las brechas (crítica>estructural>de ajuste) y de niveles (inicio>en_desarrollo)
PRIORIDAD_TIPO = {"Crítica": 3, "Estructural": 2, "De ajuste": 1}
PRIORIDAD_NIVEL = {"inicio": 2, "en_desarrollo": 1}

# Fortaleza: aspecto con >= este % en Logrado+Destacado (agrupado de lectura).
UMBRAL_FORTALEZA_PCT_LOGRADO = 60
# Muestra suficiente: mínimo de sesiones V1 válidas para no usar lenguaje
# referencial/prudente (ficha técnica, secciones 5 y 14).
UMBRAL_MUESTRA_INSUFICIENTE = 5
# Umbral de inclusión en "Aspectos que se requieren analizar con el
# colegiado" (Especificaciones Funcionales JEC §9 y §16 paso 6): un
# aspecto entra a esa tabla solo si su frecuencia de necesidad alcanza
# este %. No es un umbral de prioridad -- por debajo de él, la necesidad
# puede existir en la distribución pero no aparece en esa tabla.
UMBRAL_NECESIDAD_FRECUENTE_PCT = 30

# Cursos y cmids de docentes
CMIDS_DOCENTES_POR_CURSO: dict[int, list[int] | None] = {
    3001: [209054],
    3010: [210216],
    3013: [210234],
    3016: [210252],
    3019: [210270],
    3022: [210288],
    3025: [210306],
    3028: [210324],
    3031: [210342],
    3034: [210360],
    3037: [210378],
    3040: [210396],
    3043: [210414],
    3046: [210432],
    3049: [210450],
    3052: [210468],
    3055: [210486],
    3058: [210504],
    3061: [210522],
    3064: [210540],
    3067: [210558],
    3070: [210576],
    3073: [210594],
    3076: [210612],
}


def _cmids_docentes_configurados() -> list[int]:
    """Aplana todos los cmids de cursos que YA tienen configuración."""
    return [
        cmid
        for cmids in CMIDS_DOCENTES_POR_CURSO.values()
        if cmids is not None
        for cmid in cmids
    ]


# =============================================================================
# Acceso a datos
# =============================================================================

async def resolver_cod_modular(userid: int) -> dict:
    """
    Busca la IE del directivo por userid, trayendo las 3 columnas del
    filtro compuesto (nombre_ie, región, distrito). Retorna
    {"cod_modular": <clave compuesta>, "nombre_ie": ..., "region": ...,
    "distrito": ...} -- el campo "cod_modular" en el diccionario de
    retorno se mantiene por compatibilidad con el resto del pipeline
    (caché, logs, registrar_consulta), aunque ya no es la columna
    cod_modular de la base sino la clave compuesta armada con
    _armar_clave_ie.
    """
    query = """
        SELECT nombre_ie, "región" AS region, distrito
        FROM public."base_ebr_IA"
        WHERE userid = %(userid)s
        LIMIT 1
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(query, {"userid": userid})).fetchone()

    if row is None:
        log.warning("directivo_no_encontrado", userid=userid)
        raise DirectivoNoEncontradoError(f"userid {userid} no tiene IE asociada")

    clave_ie = _armar_clave_ie(row["nombre_ie"], row["region"], row["distrito"])

    log.info(
        "cod_modular_resuelto",
        userid=userid,
        cod_modular=clave_ie,
        nombre_ie=row["nombre_ie"],
        region=row["region"],
        distrito=row["distrito"],
    )
    return {
        "cod_modular": clave_ie,
        "nombre_ie": row["nombre_ie"],
        "region": row["region"],
        "distrito": row["distrito"],
    }


async def contar_docentes_total(nombre_ie: str, region: str, distrito: str, nivel_educativo: Optional[str] = None) -> int:
    """Cuenta docentes+jerárquicos en base_ebr_IA para esta IE, filtrando
    por las 3 columnas del filtro compuesto (nombre_ie, región, distrito)
    por separado -- no concatenadas -- para que Postgres pueda usar
    índices existentes sobre esas columnas. Si se pasa nivel_educativo,
    filtra además por ese nivel (Secundaria/Primaria/Inicial); si es
    None, cuenta los 3 niveles juntos."""
    query = """
        SELECT COUNT(*) AS total
        FROM public."base_ebr_IA"
        WHERE nombre_ie = %(nombre_ie)s
          AND "región" = %(region)s
          AND distrito IS NOT DISTINCT FROM %(distrito)s
          AND UPPER(cargo) = ANY(%(cargos)s)
          AND (%(nivel_educativo)s::text IS NULL OR nivel_educativo = %(nivel_educativo)s::text)
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query,
            {
                "nombre_ie": nombre_ie,
                "region": region,
                "distrito": distrito,
                "cargos": list(CARGOS_DOCENTE),
                "nivel_educativo": nivel_educativo,
            },
        )).fetchone()

    return row["total"] if row else 0


async def obtener_filas_vista(nombre_ie: str, region: str, distrito: str, nivel_educativo: Optional[str] = None) -> list[dict]:
    """Trae todas las filas de la vista, sin filtrar por status -- incluye
    'success' (válida), 'validation_failed' (no válida) y 'failed' (sin
    evidencia real, se trata como sin entrega). El llamador filtra según lo
    que necesite (ver filas_validas / clasificar_evidencias_por_estado).
    Filtro compuesto por las 3 columnas, igual que en base_ebr_IA. Si se
    pasa nivel_educativo, filtra además por ese nivel; si es None, trae
    los 3 niveles juntos."""
    cmids_validos = _cmids_docentes_configurados()

    if not cmids_validos:
        log.error("sin_cmids_configurados")
        raise SinDatosDisponiblesError(
            "Ningún curso de docentes tiene cmids válidos configurados todavía"
        )

    query = """
        SELECT user_id, criterion_index, nivel_obtenido, brecha, tipo_brecha, status,
               nivel_educativo, retroalimentacion
        FROM public.vw_processing_brechas_jec
        WHERE nombre_ie = %(nombre_ie)s
          AND "región" = %(region)s
          AND distrito IS NOT DISTINCT FROM %(distrito)s
          AND cmid = ANY(%(cmids_validos)s)
          AND (%(nivel_educativo)s::text IS NULL OR nivel_educativo = %(nivel_educativo)s::text)
        ORDER BY user_id, criterion_index
    """
    async with pool_datos.connection() as conn:
        rows = await (await conn.execute(
            query,
            {
                "nombre_ie": nombre_ie,
                "region": region,
                "distrito": distrito,
                "cmids_validos": cmids_validos,
                "nivel_educativo": nivel_educativo,
            },
        )).fetchall()

    _normalizar_nivel_obtenido(rows)

    log.info(
        "filas_vista_obtenidas",
        nombre_ie=nombre_ie, region=region, distrito=distrito,
        nivel_educativo=nivel_educativo, n_filas=len(rows),
    )
    return rows


# La vista en producción todavía devuelve "en_proceso" como valor de
# nivel_obtenido, aunque el resto del pipeline (F4, ficha técnica) usa la
# denominación de 4 niveles con "en_desarrollo". Se normaliza acá, en el
# único punto donde se leen las filas crudas, para que el resto del
# código (distribución, prioridad de brechas) solo vea el nombre
# canónico.
_ALIAS_NIVEL_OBTENIDO = {"en_proceso": "en_desarrollo"}


def _normalizar_nivel_obtenido(rows: list[dict]) -> None:
    for row in rows:
        row["nivel_obtenido"] = _ALIAS_NIVEL_OBTENIDO.get(row["nivel_obtenido"], row["nivel_obtenido"])


async def contar_docentes_evaluados(nombre_ie: str, region: str, distrito: str, nivel_educativo: Optional[str] = None) -> int:
    """
    Cuenta cuántos docentes DISTINTOS tienen al menos una evidencia
    'success' para esta IE, sin traer las filas completas. Más liviana
    que obtener_filas_vista + agrupar_por_docente -- usada por el
    endpoint de consulta (que debe ser rápido) para decidir la acción
    sin pagar el costo de traer todo el detalle por criterio. Si
    nivel_educativo es None, cuenta los 3 niveles juntos (usado para el
    mínimo de activación de UMBRAL_MUESTRA_INSUFICIENTE sobre la IE
    completa).
    """
    cmids_validos = _cmids_docentes_configurados()
    if not cmids_validos:
        return 0

    query = """
        SELECT COUNT(DISTINCT user_id) AS total
        FROM public.vw_processing_brechas_jec
        WHERE nombre_ie = %(nombre_ie)s
          AND "región" = %(region)s
          AND distrito IS NOT DISTINCT FROM %(distrito)s
          AND cmid = ANY(%(cmids_validos)s)
          AND status = 'success'
          AND (%(nivel_educativo)s::text IS NULL OR nivel_educativo = %(nivel_educativo)s::text)
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query,
            {
                "nombre_ie": nombre_ie,
                "region": region,
                "distrito": distrito,
                "cmids_validos": cmids_validos,
                "nivel_educativo": nivel_educativo,
            },
        )).fetchone()

    return row["total"] if row else 0


# =============================================================================
# Reglas de negocio
# =============================================================================

def agrupar_por_docente(filas: list[dict]) -> dict[int, list[dict]]:
    """Agrupa las filas crudas de la vista por user_id (docente)."""
    agrupado: dict[int, list[dict]] = {}
    for fila in filas:
        agrupado.setdefault(fila["user_id"], []).append(fila)
    return agrupado


def filas_validas(filas: list[dict]) -> list[dict]:
    """Filtra solo las filas con evidencia válida (status='success').
    Punto de uso obligatorio antes de cualquier cálculo de desempeño
    (distribución por aspecto, brechas, fortalezas): esos cálculos nunca
    deben incluir evidencia no válida o sin entrega real."""
    return [f for f in filas if f["status"] == "success"]


def _extraer_retroalimentaciones(filas_por_docente: dict[int, list[dict]]) -> list[str]:
    """
    Extrae el texto de retroalimentación de F2, una vez por docente (la
    columna 'retroalimentacion' viene repetida en las 5 filas -- una por
    criterio -- de cada docente). Se pasa como insumo agregado y sin
    identificar al docente al prompt que genera las preguntas
    movilizadoras de la RTC 1 (sección 9). Se capa a
    MAX_RETROALIMENTACIONES_EN_PROMPT para no desmedir el prompt en IEs
    con muchos docentes.
    """
    vistas = []
    for filas_docente in filas_por_docente.values():
        texto = (filas_docente[0].get("retroalimentacion") or "").strip()
        if texto:
            vistas.append(texto)
    return vistas[:MAX_RETROALIMENTACIONES_EN_PROMPT]


def clasificar_evidencias_por_estado(filas: list[dict], n_docentes_total: int) -> dict:
    """
    Clasifica las evidencias de la IE en válidas / no válidas / sin
    entrega, a partir de las filas crudas de la vista (sin filtrar por
    status) y el total de docentes registrados.

    status='success'            -> válida.
    status='validation_failed'  -> no válida (el docente entregó, pero el
                                    documento no corresponde al producto
                                    solicitado).
    status='failed'              -> sin evidencia real que evaluar (no se
                                    pudo ubicar el documento o se ingresó
                                    mal) -- se cuenta junto con quienes no
                                    tienen ninguna fila, como "sin entrega".

    Se asume un status consistente por docente (todas sus filas comparten
    el mismo status de su intento de entrega).
    """
    por_docente = agrupar_por_docente(filas)
    n_validas = 0
    n_no_validas = 0
    for filas_docente in por_docente.values():
        status = filas_docente[0]["status"]
        if status == "success":
            n_validas += 1
        elif status == "validation_failed":
            n_no_validas += 1
        # status == 'failed' (o cualquier otro) no suma aquí -- cae en
        # n_sin_entrega junto con los docentes sin ninguna fila.

    n_sin_entrega = max(n_docentes_total - n_validas - n_no_validas, 0)
    return {
        "n_validas": n_validas,
        "n_no_validas": n_no_validas,
        "n_sin_entrega": n_sin_entrega,
    }


def seleccionar_brechas_globales(filas_docente: list[dict]) -> list[str]:
    """
    5 reglas de prioridad para reducir las brechas de un docente a máximo 2:
      1. Crítica > Estructural > De ajuste.
      2. Dentro del mismo tipo, Inicio > En proceso.
      3. Empate total -> gana el criterion_index más bajo.
      4. Máximo 2 brechas.
      5. Si ninguna es Crítica ni Estructural, no se selecciona ninguna
         (no se fuerza una de ajuste solo para completar el cupo).
    """
    candidatas = [f for f in filas_docente if f.get("brecha") is not None]
    if not candidatas:
        return []

    hay_critica_o_estructural = any(
        f["tipo_brecha"] in ("Crítica", "Estructural") for f in candidatas
    )
    if not hay_critica_o_estructural:
        return []

    candidatas_ordenadas = sorted(
        candidatas,
        key=lambda f: (
            -PRIORIDAD_TIPO.get(f["tipo_brecha"], 0),
            -PRIORIDAD_NIVEL.get(f["nivel_obtenido"], 0),
            f["criterion_index"],
        ),
    )
    return [f["brecha"] for f in candidatas_ordenadas[:2]]


def calcular_distribucion_por_criterio(filas: list[dict]) -> list[dict]:
    """Distribución de las filas VÁLIDAS por aspecto (criterion_index), en
    los 4 niveles de la rúbrica (inicio, en_desarrollo, logrado, destacado)
    más los dos agrupados de lectura del documento: "Inicio + En
    desarrollo" (requiere fortalecimiento) y "Logrado + Destacado"
    (desempeño favorable). El llamador es responsable de pasar solo filas
    válidas (ver filas_validas)."""
    por_criterio: dict[int, dict[str, int]] = {}

    for fila in filas:
        c = fila["criterion_index"]
        nivel = fila["nivel_obtenido"]
        conteos = por_criterio.setdefault(
            c, {"inicio": 0, "en_desarrollo": 0, "logrado": 0, "destacado": 0}
        )
        if nivel in conteos:
            conteos[nivel] += 1

    resultado = []
    for criterio_id, conteos in sorted(por_criterio.items()):
        total = sum(conteos.values())
        if total == 0:
            continue
        pct_inicio = round(100 * conteos["inicio"] / total)
        pct_en_desarrollo = round(100 * conteos["en_desarrollo"] / total)
        pct_logrado = round(100 * conteos["logrado"] / total)
        pct_destacado = round(100 * conteos["destacado"] / total)
        resultado.append({
            "criterio_id": f"C{criterio_id}",
            "n": total,
            # Conteos crudos (no derivados de porcentajes redondeados) --
            # necesarios para poder citar "[n] de [N]" con el número
            # EXACTO de sesiones en el agrupado predominante, sin que el
            # LLM tenga que reconstruirlo a partir del %.
            "n_inicio": conteos["inicio"],
            "n_en_desarrollo": conteos["en_desarrollo"],
            "n_logrado": conteos["logrado"],
            "n_destacado": conteos["destacado"],
            "pct_inicio": pct_inicio,
            "pct_en_desarrollo": pct_en_desarrollo,
            "pct_logrado": pct_logrado,
            "pct_destacado": pct_destacado,
            "pct_inicio_en_desarrollo": pct_inicio + pct_en_desarrollo,
            "pct_logrado_destacado": pct_logrado + pct_destacado,
        })
    return resultado


def _primera_oracion_interpretacion(c: dict) -> str:
    """
    Arma en Python -- con el conteo EXACTO, no derivado de un porcentaje
    redondeado -- la primera oración de la interpretación de un aspecto
    (sección 3): "En [n] de [N] sesiones ([pct]%) se observa [resultado]."
    El LLM NUNCA escribe esta oración (ni sus números): solo redacta la
    explicación pedagógica y la fuente que van después. Así se elimina de
    raíz el riesgo de que el modelo invente o desalinee una cifra frente
    a la tabla de la sección 2 -- no hay instrucción de prompt que pueda
    garantizar esto de forma confiable, solo no dejarlo en sus manos.
    """
    n_total = c["n"]
    if c["pct_logrado_destacado"] >= c["pct_inicio_en_desarrollo"]:
        n_grupo = c["n_logrado"] + c["n_destacado"]
        pct_grupo = c["pct_logrado_destacado"]
        resultado = "Logrado o Destacado"
    else:
        n_grupo = c["n_inicio"] + c["n_en_desarrollo"]
        pct_grupo = c["pct_inicio_en_desarrollo"]
        resultado = "Inicio o En desarrollo"
    return f"En {n_grupo} de {n_total} sesiones ({pct_grupo}%) se observa {resultado}."


def calcular_frecuencia_brechas(brechas_por_docente: dict[int, list[str]]) -> dict[str, dict]:
    n_docentes = len(brechas_por_docente)
    if n_docentes == 0:
        return {}

    conteos: dict[str, int] = {}
    for lista_brechas in brechas_por_docente.values():
        for brecha_id in lista_brechas:
            conteos[brecha_id] = conteos.get(brecha_id, 0) + 1

    return {
        brecha_id: {"conteo": conteo, "pct": round(100 * conteo / n_docentes)}
        for brecha_id, conteo in conteos.items()
    }


def clasificar_fortalezas(distribucion: list[dict]) -> list[dict]:
    """Fortaleza: aspecto con >= UMBRAL_FORTALEZA_PCT_LOGRADO% en el
    agrupado de lectura 'Logrado + Destacado' (ficha técnica, sección 5:
    "Logrado + Destacado" es un recurso de lectura, no un nivel nuevo)."""
    return [
        {"id": c["criterio_id"], "n": c["n"], "pct": c["pct_logrado_destacado"]}
        for c in distribucion
        if c["pct_logrado_destacado"] >= UMBRAL_FORTALEZA_PCT_LOGRADO
    ]


def clasificar_necesidades_frecuentes(frecuencia_brechas: dict[str, dict]) -> list[dict]:
    """Aspectos que se requieren analizar con el colegiado (Especificaciones
    Funcionales JEC §9, §10 sección 5, §16 paso 6): solo las brechas
    definidas por F2 (brechas_identificadas[]) cuya frecuencia alcanza
    UMBRAL_NECESIDAD_FRECUENTE_PCT -- el umbral es un FILTRO de inclusión
    en la tabla, no solo un umbral de lectura. No se infieren brechas
    nuevas -- se usa tal cual lo que ya calculó
    seleccionar_brechas_globales/calcular_frecuencia_brechas. Puede
    devolver una lista vacía si ninguna alcanza el umbral."""
    necesidades = [
        {"id": brecha_id, "n": info["conteo"], "pct": info["pct"]}
        for brecha_id, info in frecuencia_brechas.items()
        if info["pct"] >= UMBRAL_NECESIDAD_FRECUENTE_PCT
    ]
    necesidades.sort(key=lambda n: -n["pct"])
    return necesidades


def calcular_enfasis_lectura(distribucion_por_id: dict[str, dict]) -> list[dict]:
    """Énfasis del programa (ficha técnica, sección 7): se leen a partir
    de los aspectos ya evaluados por F2, sin crear indicadores que F2 no
    genera. Comprensión lectora no tiene indicador independiente en la
    rúbrica de la Sesión V1 -- su texto es fijo, tal como exige el
    documento (no se genera un % institucional para ese énfasis)."""
    enfasis = []
    c3 = distribucion_por_id.get("C3")

    if c3:
        favorable = c3["pct_logrado_destacado"] >= c3["pct_inicio_en_desarrollo"]
        texto_involucramiento = (
            f"Las actividades previstas ofrecen oportunidades favorables para participar, "
            f"analizar, decidir y producir: {c3['pct_logrado_destacado']}% de las sesiones "
            "revisadas se ubica en Logrado o Destacado en demanda cognitiva e involucramiento "
            "activo."
            if favorable else
            f"En {c3['pct_inicio_en_desarrollo']}% de las sesiones revisadas, la demanda "
            "cognitiva y el involucramiento activo de los estudiantes todavía requiere mayor "
            "fortalecimiento."
        )
        enfasis.append({
            "enfasis": "Involucramiento activo",
            "aspecto_relacionado": "C3",
            "texto_permitido": texto_involucramiento,
        })
        enfasis.append({
            "enfasis": "Pensamiento crítico",
            "aspecto_relacionado": "C2/C3",
            "texto_permitido": (
                "La planificación de una parte de las sesiones revisadas puede ofrecer mayores "
                "oportunidades para analizar, interpretar, tomar decisiones, argumentar y "
                "elaborar respuestas propias."
            ),
        })

    enfasis.append({
        "enfasis": "Comprensión lectora",
        "aspecto_relacionado": None,
        "texto_permitido": (
            "Este reporte no presenta un porcentaje institucional de comprensión lectora "
            "porque la rúbrica de la Sesión V1 no la evalúa como un criterio independiente. "
            "Su análisis debe complementarse con evidencias específicas de aprendizaje de los "
            "estudiantes."
        ),
    })
    enfasis.append({
        # Denominación oficial de las Especificaciones Funcionales JEC §7
        # ("Resolución de problemas"). El sistema en producción venía
        # usando "Aprendizaje basado en situaciones y problemas" -- se
        # corrige por indicación directa del usuario para eliminar el
        # desfase con el documento entregado a TI.
        "enfasis": "Resolución de problemas",
        "aspecto_relacionado": "C1/C2",
        "texto_permitido": (
            "Las situaciones significativas y la demanda cognitiva son aspectos relevantes "
            "para analizar cómo se están generando oportunidades de aprendizaje profundo; no "
            "se genera un porcentaje independiente para este énfasis."
        ),
    })

    return enfasis


FUENTES_CONTRASTE_POR_ASPECTO = {
    "C1": "MPE y otras evidencias de planificación",
    "C2": "MPE, ENLA y evidencias de aprendizaje de los estudiantes",
    "C3": "MPE, ENLA y evidencias de aprendizaje de los estudiantes",
    "C4": "MPE, instrumentos de evaluación y producciones de los estudiantes",
    "C5": "MPE y evidencias de aula",
}

PREGUNTAS_CONTRASTE_POR_ASPECTO = {
    "C1": "¿Qué decisiones de planificación pueden mantenerse y compartirse para fortalecer otros aspectos?",
    "C2": "¿Qué relación encontramos entre las oportunidades que brindan las experiencias de aprendizaje y lo que muestran los estudiantes en sus aprendizajes?",
    "C3": "¿Lo previsto en las sesiones se corresponde con lo que ocurre durante la interacción en el aula?",
    "C4": "¿Cómo se recoge y utiliza la información sobre el aprendizaje para ajustar la enseñanza?",
    "C5": "¿Los recursos y espacios previstos se aprovechan efectivamente durante la sesión?",
}


def calcular_aspectos_contraste(distribucion: list[dict], fortalezas: list[dict]) -> list[dict]:
    """Sección 8 -- 'Lo que conviene contrastar con otras evidencias'
    (banco de redacción, punto 12, plantilla 'Contraste'). Se arma en
    Python, sin LLM: toma los aspectos con mayor % en Inicio+En desarrollo
    (necesitan fortalecimiento) y la principal fortaleza, y les asocia la
    fuente y pregunta de contraste correspondientes."""
    aspectos = []

    ordenados = sorted(distribucion, key=lambda c: -c["pct_inicio_en_desarrollo"])
    for c in ordenados[:2]:
        if c["pct_inicio_en_desarrollo"] <= 0:
            continue
        cid = c["criterio_id"]
        aspectos.append({
            "hallazgo": (
                f"{_nombre_criterio(cid)}: {c['pct_inicio_en_desarrollo']}% de las sesiones "
                "revisadas requiere mayor fortalecimiento."
            ),
            "fuente_sugerida": FUENTES_CONTRASTE_POR_ASPECTO.get(cid, "MPE y ENLA"),
            "pregunta": PREGUNTAS_CONTRASTE_POR_ASPECTO.get(
                cid, "¿Qué conviene revisar para comprender mejor esta situación?"
            ),
        })

    if fortalezas:
        top = max(fortalezas, key=lambda f: f["pct"])
        cid = top["id"]
        aspectos.append({
            "hallazgo": f"{_nombre_criterio(cid)} aparece como una fortaleza ({top['pct']}% en Logrado o Destacado).",
            "fuente_sugerida": FUENTES_CONTRASTE_POR_ASPECTO.get(cid, "MPE"),
            "pregunta": PREGUNTAS_CONTRASTE_POR_ASPECTO.get(
                cid, "¿Qué decisiones de planificación conviene preservar?"
            ),
        })

    return aspectos


# Sección 9 -- preguntas movilizadoras para la RTC 1. El camino normal es
# que el LLM las genere a partir de las retroalimentaciones reales de los
# docentes (ver _construir_prompt_secciones_narrativas / campo
# "retroalimentacion" de la vista) -- esta lista fija es solo el
# fallback si el LLM falla dos veces seguidas (ver
# _secciones_narrativas_fallback).
PREGUNTAS_RTC_FIJAS = (
    "¿Qué información de este reporte se confirma cuando la contrastamos con MPE, ENLA y las "
    "evidencias de aprendizaje de nuestros estudiantes?",
    "¿Qué oportunidades estamos ofreciendo actualmente a los estudiantes para comprender, "
    "analizar, argumentar, tomar decisiones y producir respuestas propias?",
    "¿Qué diferencias encontramos entre lo que se planifica en las sesiones y lo que "
    "observamos en la práctica real de aula?",
    "¿Qué decisiones de planificación que ya están funcionando favorablemente podemos "
    "aprovechar para fortalecer otros aspectos?",
    "¿Qué información adicional necesitamos revisar antes de definir la necesidad que la "
    "institución debe priorizar?",
)


# =============================================================================
# Redacción del texto narrativo (LLM)
# =============================================================================

def _enriquecer_distribucion_con_nombre(distribucion: list[dict]) -> list[dict]:
    return [{**c, "nombre": _nombre_criterio(c["criterio_id"])} for c in distribucion]


def _enriquecer_necesidades_con_nombre(necesidades: list[dict]) -> list[dict]:
    return [{**n, "nombre": _nombre_brecha(n["id"])} for n in necesidades]


def _enriquecer_fortalezas_con_nombre(fortalezas: list[dict]) -> list[dict]:
    return [{**f, "nombre": _nombre_criterio(f["id"])} for f in fortalezas]


# Tope de retroalimentaciones que se envían al prompt -- evita un prompt
# desmedido en IEs con muchos docentes; una muestra representativa basta
# para que el LLM identifique temas recurrentes.
MAX_RETROALIMENTACIONES_EN_PROMPT = 40


def _construir_prompt_secciones_narrativas(v: dict) -> str:
    """
    Arma el prompt para las TRES piezas de texto que sí requieren
    elaboración a partir de lenguaje libre (no son un fill-in-the-blank
    determinista): la interpretación de cada uno de los 5 aspectos
    (sección 3 del reporte), las preguntas movilizadoras para la RTC 1
    (sección 9, a partir de las retroalimentaciones reales de F2) y la
    síntesis institucional (sección 10). Todo lo demás del reporte
    (tablas, énfasis, necesidades, fortalezas, contraste) se arma en
    Python -- ver generar_reporte.

    Reglas duras (ficha técnica F4, sección 11): sin las palabras
    "brecha/alerta/patrón/clúster/semaforización/nivel global", sin
    códigos C1-C5/B1-B5, sin lenguaje causal, sin "la mayoría" si la
    muestra es menor de 5, cifras siempre con n y %, distinguir "las
    sesiones muestran" de "la institución presenta", sin recomendaciones
    individuales al docente, sin markdown ni placeholders.
    """
    distribucion = v["distribucion_por_aspecto"]
    necesidades = v["necesidades_frecuentes"]
    fortalezas = v["fortalezas"]
    retroalimentaciones = v.get("retroalimentaciones", [])

    aspectos_texto = "\n".join(
        f"- {c['nombre']} (id interno {c['criterio_id']}, evalúa: "
        f"{CRITERIOS.get(c['criterio_id'], {}).get('que_evalua', '')}): {c['n']} sesiones "
        f"válidas -- Inicio {c['pct_inicio']}%, En desarrollo {c['pct_en_desarrollo']}%, "
        f"Logrado {c['pct_logrado']}%, Destacado {c['pct_destacado']}% "
        f"(Inicio+En desarrollo {c['pct_inicio_en_desarrollo']}%, "
        f"Logrado+Destacado {c['pct_logrado_destacado']}%). "
        f"PRIMERA ORACIÓN YA REDACTADA por el sistema, con las cifras "
        f"exactas (NO la repitas, NO la reescribas, NO cites tú ningún "
        f"número para este aspecto -- tu texto se coloca DESPUÉS de "
        f"ella): \"{_primera_oracion_interpretacion(c)}\""
        for c in distribucion
    )
    necesidades_texto = "\n".join(
        f"- {n['nombre']} (id interno {n['id']}): presente en {n['n']} de "
        f"{v['n_evidencias_validas']} sesiones ({n['pct']}%)"
        for n in necesidades
    ) or "- Ninguna necesidad frecuente identificada."
    fortalezas_texto = "\n".join(
        f"- {f['nombre']} (id interno {f['id']}): {f['n']} de {v['n_evidencias_validas']} "
        f"sesiones ({f['pct']}%) en Logrado o Destacado"
        for f in fortalezas
    ) or "- Ninguna alcanzó el umbral de fortaleza."
    retroalimentaciones_texto = "\n".join(f"- {r}" for r in retroalimentaciones) or (
        "- Sin retroalimentaciones registradas para esta IE."
    )

    # Corrección pedagógica (Especificaciones Funcionales JEC §9, §11,
    # §12): el reporte NUNCA señala un aspecto como "el que requiere
    # mayor atención" ni una necesidad como "la más frecuente" en tono de
    # conclusión -- decidir qué atender primero es del directivo con su
    # colegiado en la RTC. Por eso este listado para la síntesis NO se
    # ordena por % (se mantiene el orden oficial C1→C5) y no lleva
    # porcentajes: son los aspectos que NO llegaron al umbral de
    # fortaleza, mencionados de forma neutra.
    ids_fortalezas_set = {f["id"] for f in fortalezas}
    aspectos_desarrollo = [
        c["nombre"] for c in distribucion if c["criterio_id"] not in ids_fortalezas_set
    ]
    aspectos_desarrollo_texto = ", ".join(aspectos_desarrollo) or (
        "(todos los aspectos evaluados alcanzaron el umbral de fortaleza; omite esta cláusula)"
    )

    # Igual que con "aspectos con distinto nivel de desarrollo": si hay
    # varias fortalezas, el LLM no debe elegir cuál es "la principal" (ni
    # inventar una si no hay ninguna) -- se resuelve acá, de forma
    # determinista, y se le entrega ya resuelta.
    fortaleza_principal = max(fortalezas, key=lambda f: f["pct"]) if fortalezas else None
    fortaleza_principal_texto = (
        f"{fortaleza_principal['nombre']} (id interno {fortaleza_principal['id']})"
        if fortaleza_principal
        else "(ninguna fortaleza alcanzó el umbral; omite la cláusula de fortaleza -- ver instrucción de síntesis)"
    )

    ids_criterios = ", ".join(f'"{c["criterio_id"]}"' for c in distribucion)

    def _schema_manifestaciones(items: list[dict]) -> str:
        # Evita mostrarle al LLM un placeholder tipo "<id>" cuando la
        # lista viene vacía -- eso lo tentaba a inventar una clave falsa.
        if not items:
            return "{}"
        return "{ " + ", ".join(f'"{i["id"]}": "manifestación breve"' for i in items) + " }"

    schema_fortalezas = _schema_manifestaciones(fortalezas)
    schema_necesidades = _schema_manifestaciones(necesidades)

    return f"""
Eres un redactor pedagógico para el sistema educativo peruano. Vas a
redactar piezas de un reporte institucional para el directivo de una IE,
a partir de datos YA CALCULADOS (no recalcules ni inventes cifras).

Datos generales:
- IE: {v.get('nombre_ie')}
- Sesiones V1 válidas: {v.get('n_evidencias_validas')} de {v.get('n_docentes_total')} docentes registrados ({v.get('pct_cobertura')}% de cobertura)
- Muestra suficiente (>= 5 sesiones válidas): {v.get('muestra_suficiente')}

Resultados por aspecto evaluado:
{aspectos_texto}

Aspectos que se requieren analizar con el colegiado (solo los que
alcanzan el umbral de frecuencia -- pueden ser ninguno):
{necesidades_texto}

Fortalezas identificadas:
{fortalezas_texto}

Fortaleza principal para la síntesis (ya resuelta -- no elijas ni
calcules otra, aunque haya varias fortalezas en la lista de arriba):
{fortaleza_principal_texto}

Aspectos con distinto nivel de desarrollo, en orden neutro -- para
mencionar EN CONJUNTO en el segundo párrafo de la síntesis, SIN elegir
ni destacar uno como más urgente que otro, y SIN porcentajes (ya viene
resuelto, no ordenes ni filtres tú):
{aspectos_desarrollo_texto}

Retroalimentaciones registradas para los docentes de esta IE (texto libre
de F2, ya agregado -- NUNCA estén asociadas a un docente identificable en
tu respuesta, úsalas solo para reconocer temas recurrentes):
{retroalimentaciones_texto}

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional antes
ni después, con esta forma exacta:
{{
  "interpretaciones": {{ {ids_criterios}: "SOLO explicación + fuente, sin la primera oración ni cifras", ... }},
  "manifestaciones": {{
    "fortalezas": {schema_fortalezas},
    "necesidades": {schema_necesidades}
  }},
  "preguntas_rtc": ["pregunta 1", "pregunta 2", "..."],
  "sintesis_institucional": "texto de síntesis"
}}

Para cada "interpretaciones.<id>" (uno por cada aspecto listado arriba),
escribe ÚNICAMENTE la continuación de la "PRIMERA ORACIÓN YA REDACTADA"
que aparece junto a ese aspecto -- NO la incluyas tú, el sistema ya la
antepone automáticamente por fuera. Tu texto son las dos oraciones
siguientes de la plantilla del banco de redacción: "[explicación
pedagógica]. Para comprender mejor esta situación, conviene contrastarla
con [fuente]." Es decir, "interpretaciones.<id>" debe EMPEZAR directo con
la explicación (ej. "Esto significa que..."), nunca con "En [n] de
[N]..." ni con ningún número -- todas las cifras de este aspecto ya
están en la primera oración que el sistema antepone, y citar otra ahí
duplicaría o contradiría esa cifra.
- Para la explicación: escribe algo sustancioso y específico de 1-2
  frases, apoyado en lo que "evalúa" ese aspecto (ver arriba) -- no un
  relleno genérico ni una frase de una sola línea. Esta explicación es
  cualitativa, NUNCA lleva cifras ni porcentajes.
- Cierra con una oración de contraste usando una fuente de MPE/ENLA/
  evidencias de aprendizaje/otras según corresponda.

Para cada "manifestaciones.fortalezas.<id>" (uno por cada fortaleza
listada arriba) y "manifestaciones.necesidades.<id>" (uno por cada
necesidad listada arriba), escribe SOLO el fragmento de "manifestación"
pedagógica -- una frase breve en minúscula que complete naturalmente,
SIN repetir el nombre del aspecto ni las cifras (eso ya lo arma el
sistema por fuera): para fortalezas, algo que podría continuar "Las
evidencias muestran ..."; para necesidades, algo que podría continuar
"En estas sesiones se observa ...". Ejemplo de fortaleza: "una relación
clara y consistente entre el propósito de la sesión y el desafío
planteado a los estudiantes". Ejemplo de necesidad: "actividades que
ofrecen oportunidades limitadas para que los estudiantes analicen,
decidan o argumenten". NUNCA antepongas una etiqueta como "Fortaleza:"
o "Necesidad:" -- es solo el fragmento de texto, no una oración
completa ni un título.

Para "preguntas_rtc", genera de 4 a 6 preguntas movilizadoras abiertas
para preparar la RTC 1, orientadas al análisis colectivo del equipo
docente. Básalas en los TEMAS RECURRENTES que reconozcas en las
retroalimentaciones registradas arriba (no inventes temas que no
aparezcan en ellas) y en los resultados por aspecto. Cada pregunta debe
ser general para el equipo (nunca mencionar ni describir a un docente en
particular, ni citar textualmente una retroalimentación) y debe invitar a
contrastar la información con MPE, ENLA u otras evidencias, no a asignar
responsabilidades individuales. NUNCA cites un porcentaje ni una cifra
exacta dentro de una pregunta -- son preguntas abiertas, no afirmaciones
de dato; si quieres referirte a un aspecto, nómbralo sin número.

Para "sintesis_institucional", redacta TRES párrafos (separados por un
salto de línea en blanco, sin encabezados ni numeración), siguiendo esta
estructura como base (banco de redacción del programa):
1. Panorama cualitativo: usa EXACTAMENTE la "Fortaleza principal para la
   síntesis" de arriba (ya resuelta -- no elijas ni calcules otra, ni
   siquiera si hay varias fortalezas) y qué aspectos requieren seguir
   fortaleciéndose (las necesidades), en prosa, sin cifras todavía. Si el
   dato dice que ninguna fortaleza alcanzó el umbral, omite la cláusula
   de fortaleza y empieza el párrafo directamente por los aspectos que
   requieren fortalecerse.
2. Distinto nivel de desarrollo: menciona EN CONJUNTO, sin ordenarlos ni
   destacar uno como más urgente que otro, los aspectos que aparecen en
   "Aspectos con distinto nivel de desarrollo" arriba (usa exactamente
   esa lista; si viene vacía, omite este párrafo). No agregues
   porcentajes aquí -- son un panorama cualitativo, no una comparación.
   Cierra indicando que esta información es relevante para el
   diagnóstico, y que decidir qué aspecto analizar primero corresponde
   al directivo junto con su colegiado, en la RTC.
3. Contraste y propósito: indica que, para avanzar hacia una comprensión
   institucional, corresponde contrastar estos resultados con MPE, ENLA
   y las evidencias de aprendizaje de los estudiantes; plantea la
   pregunta central que conviene sostener entre las condiciones que
   ofrecen las experiencias de aprendizaje y las necesidades de
   aprendizaje de los estudiantes; y cierra señalando que esa
   contrastación permitirá fundamentar las decisiones del Diagnóstico
   institucional y, después, de la RTC 1.
Si tanto la fortaleza principal como la lista de aspectos con distinto
nivel de desarrollo vienen vacías (caso límite, ej. muestra muy pequeña),
redacta un primer párrafo breve que solo mencione la cobertura, sin
inventar una fortaleza ni un aspecto que no esté en los datos, y sigue
igual con los párrafos 2 (omitido si no aplica) y 3.

Reglas estrictas, sin excepción:
- NUNCA uses las palabras "brecha", "brechas", "alerta", "alertas",
  "patrón", "patrones", "clúster", "semaforización" ni "nivel global".
- NUNCA muestres los códigos internos (C1, C2, C3, C4, C5, B1, B2, B3,
  B4, B5) en el texto -- usa siempre el nombre del aspecto o necesidad
  tal como aparece arriba.
- NUNCA uses lenguaje causal ("esto provoca", "esto explica", "la causa
  es", "debido a que"). Los datos no permiten establecer causalidad.
- NUNCA formules recomendaciones ni menciones individuales a un docente
  (ni en las interpretaciones ni en las preguntas): el destinatario es
  siempre el directivo, y las cifras y temas son agregados.
- Distingue siempre entre "las sesiones muestran/revelan" (lo que dice
  la evidencia) y "la institución presenta/tiene" (una conclusión
  institucional) -- prefiere la primera forma salvo que la cobertura sea
  alta y el dato sea claramente representativo.
- Si "Muestra suficiente" es falso, evita "la mayoría" y cualquier
  generalización a toda la institución; usa formulaciones referenciales
  ("en las sesiones revisadas...").
- Toda cifra que menciones (n y %) debe existir tal cual en los datos de
  arriba -- no la recalcules ni la combines entre aspectos distintos.
- Sin markdown (sin **negritas**, sin #, sin viñetas). Sin placeholders
  entre corchetes. Sin fecha, sin "Dirigido a:", sin "Atentamente".
- Todo texto que redactes es prosa fluida: NINGÚN campo debe empezar con
  una etiqueta o encabezado tipo "Síntesis:", "Contraste:", "Fortaleza:",
  "Necesidad:" ni similar -- el nombre de la sección ya lo pone el
  reporte por fuera, el texto que redactas es solo el contenido.
- NUNCA presentes un aspecto como "el que requiere mayor atención", "el
  más urgente" o "el prioritario", ni una necesidad como "la más
  frecuente", en tono de conclusión -- ni en las interpretaciones, ni en
  las preguntas RTC, ni en la síntesis. El reporte muestra el estado
  encontrado en las evidencias; decidir qué atender primero es una
  decisión del directivo con su colegiado, en la RTC.
- Usa siempre "propósito de aprendizaje", NUNCA "objetivos de
  aprendizaje" -- especialmente en la interpretación de "Coherencia
  entre propósito y desafíos", para mantener coherencia con el nombre
  oficial de ese aspecto.
- No sugieras acciones, estrategias ni próximos pasos concretos: el
  reporte describe, la decisión queda en manos del directivo.
""".strip()


_MANIFESTACION_GENERICA = "resultados que conviene revisar con mayor detalle junto con otras evidencias"


def _interpretacion_fallback(c: dict) -> str:
    """Interpretación completa (primera oración con cifras exactas +
    cuerpo mínimo sin elaboración pedagógica), usada para completar
    huecos puntuales de la respuesta del LLM o como degradación total si
    el LLM falla dos veces."""
    return (
        f"{_primera_oracion_interpretacion(c)} Esto se reporta a partir de los datos "
        "disponibles. Para comprender mejor esta situación, conviene contrastarla con "
        "las evidencias de aula."
    )


# Por si el LLM ignora la instrucción de no escribir la primera oración
# con cifras y de todos modos la incluye -- se recorta para no duplicar
# ni contradecir la oración que Python ya antepone (ver
# _completar_huecos_narrativos).
_PATRON_ORACION_NUMERICA_INICIAL = re.compile(
    r"^\s*En\s+\d+\s+de\s+\d+\s+sesiones\s*\([^)]*\)[^.]*\.\s*", re.IGNORECASE
)


def _quitar_oracion_numerica_inicial(texto: str) -> str:
    return _PATRON_ORACION_NUMERICA_INICIAL.sub("", texto, count=1).strip()


def _sintesis_fallback(variables: dict) -> str:
    """Síntesis institucional mínima determinista, para el mismo caso que
    _interpretacion_fallback."""
    return (
        f"La revisión de las Sesiones V1 de {variables.get('nombre_ie', 'la institución')} reúne "
        f"{variables.get('n_evidencias_validas')} de {variables.get('n_docentes_total')} sesiones "
        f"válidas ({variables.get('pct_cobertura')}% de cobertura). Estos resultados aportan una "
        "línea de base para el Diagnóstico institucional; su interpretación requiere "
        "contrastarlos con las demás evidencias disponibles."
    )


def _secciones_narrativas_fallback(variables: dict) -> dict:
    """Degradación total: se usa solo cuando el LLM ni siquiera devuelve
    un JSON parseable después de 2 intentos. Texto plantillado mínimo en
    todos los campos, para no romper el reporte completo."""
    return {
        "interpretaciones": {
            c["criterio_id"]: _interpretacion_fallback(c) for c in variables["distribucion_por_aspecto"]
        },
        "manifestaciones": {
            "fortalezas": {f["id"]: _MANIFESTACION_GENERICA for f in variables["fortalezas"]},
            "necesidades": {n["id"]: _MANIFESTACION_GENERICA for n in variables["necesidades_frecuentes"]},
        },
        "preguntas_rtc": list(PREGUNTAS_RTC_FIJAS),
        "sintesis_institucional": _sintesis_fallback(variables),
    }


def _completar_huecos_narrativos(variables: dict, data: dict) -> dict:
    """
    Completa SOLO los campos puntuales que el LLM haya omitido, en vez de
    descartar toda la respuesta por un campo faltante (antes, si por
    ejemplo faltaba una sola manifestación de necesidad, se tiraban
    también las 5 interpretaciones y la síntesis, que sí venían bien, y
    se gastaba un reintento completo para nada). Registra qué campos
    tuvo que completar, para poder monitorear qué tan seguido pasa.
    """
    interpretaciones = dict(data.get("interpretaciones") or {})
    manifestaciones_crudas = data.get("manifestaciones") or {}
    manifestaciones = {
        "fortalezas": dict(manifestaciones_crudas.get("fortalezas") or {}),
        "necesidades": dict(manifestaciones_crudas.get("necesidades") or {}),
    }
    preguntas_rtc = data.get("preguntas_rtc")
    sintesis = data.get("sintesis_institucional")

    faltantes = []

    for c in variables["distribucion_por_aspecto"]:
        cuerpo_llm = interpretaciones.get(c["criterio_id"])
        if cuerpo_llm:
            # Python es dueño exclusivo de la primera oración (cifras
            # exactas) -- se antepone siempre, sin importar lo que haya
            # escrito el LLM. Si el LLM igual escribió su propia versión
            # con números al inicio (ignorando la instrucción), se
            # recorta para no duplicar ni contradecir la de Python.
            cuerpo_llm = _quitar_oracion_numerica_inicial(cuerpo_llm)
            interpretaciones[c["criterio_id"]] = f"{_primera_oracion_interpretacion(c)} {cuerpo_llm}"
        else:
            interpretaciones[c["criterio_id"]] = _interpretacion_fallback(c)
            faltantes.append(f"interpretaciones.{c['criterio_id']}")

    for f in variables["fortalezas"]:
        if not manifestaciones["fortalezas"].get(f["id"]):
            manifestaciones["fortalezas"][f["id"]] = _MANIFESTACION_GENERICA
            faltantes.append(f"manifestaciones.fortalezas.{f['id']}")

    for n in variables["necesidades_frecuentes"]:
        if not manifestaciones["necesidades"].get(n["id"]):
            manifestaciones["necesidades"][n["id"]] = _MANIFESTACION_GENERICA
            faltantes.append(f"manifestaciones.necesidades.{n['id']}")

    if not isinstance(preguntas_rtc, list) or not preguntas_rtc:
        preguntas_rtc = list(PREGUNTAS_RTC_FIJAS)
        faltantes.append("preguntas_rtc")

    if not sintesis:
        sintesis = _sintesis_fallback(variables)
        faltantes.append("sintesis_institucional")

    if faltantes:
        log.warning(
            "llm_secciones_narrativas_incompletas",
            cod_modular=variables.get("cod_modular"),
            campos_completados_con_fallback=faltantes,
        )

    return {
        "interpretaciones": interpretaciones,
        "manifestaciones": manifestaciones,
        "preguntas_rtc": preguntas_rtc,
        "sintesis_institucional": sintesis,
    }


async def redactar_secciones_narrativas(variables: dict) -> dict:
    """Llama al LLM UNA vez por reporte para redactar las interpretaciones
    por aspecto, las manifestaciones, las preguntas RTC y la síntesis
    institucional -- las únicas piezas que requieren elaboración
    pedagógica real; todo lo demás del reporte se arma en Python. Si el
    LLM no devuelve un JSON parseable, reintenta una vez; si vuelve a
    fallar, degrada TODO a texto plantillado mínimo. Si el JSON es válido
    pero le faltan campos puntuales, esos huecos se completan sin
    descartar el resto de la respuesta (ver _completar_huecos_narrativos)."""
    prompt = _construir_prompt_secciones_narrativas(variables)

    for intento in (1, 2):
        inicio = time.monotonic()
        try:
            crudo = await generar_texto_narrativo(prompt, formato_json=True)
            data = json.loads(crudo)
            if not isinstance(data, dict):
                raise ValueError("la respuesta del LLM no es un objeto JSON")
        except Exception:
            duracion_ms = round((time.monotonic() - inicio) * 1000)
            log.error(
                "llm_secciones_narrativas_fallidas",
                cod_modular=variables.get("cod_modular"),
                intento=intento,
                duracion_ms=duracion_ms,
                exc_info=True,
            )
            if intento == 2:
                return _secciones_narrativas_fallback(variables)
            continue

        duracion_ms = round((time.monotonic() - inicio) * 1000)
        log.info(
            "llm_secciones_narrativas_generadas",
            cod_modular=variables.get("cod_modular"),
            duracion_ms=duracion_ms,
        )
        return _completar_huecos_narrativos(variables, data)


def _sin_punto_final(texto: str) -> str:
    """El LLM a veces devuelve la manifestación ya terminada en punto;
    esta plantilla siempre le agrega su propio punto final, así que se
    recorta acá para no terminar con '..'."""
    return texto.rstrip().rstrip(".")


# =============================================================================
# Orquestador de generación
# =============================================================================

async def generar_reporte(cod_modular: str, nombre_ie: str, region: str, distrito: str) -> dict:
    """
    Genera el Reporte Institucional 1 (F4/JEC): un único análisis
    institucional para la IE completa.

    cod_modular es la CLAVE COMPUESTA (nombre_ie|región|distrito), usada
    como institución y para logs/caché. nombre_ie, region, distrito por
    separado son los que se usan en las queries reales contra
    base_ebr_IA y vw_processing_brechas_jec.
    """
    todas_las_filas = await obtener_filas_vista(nombre_ie, region, distrito)
    if not todas_las_filas:
        raise SinDatosDisponiblesError(f"Sin datos en la vista para {cod_modular}")

    n_docentes_total = await contar_docentes_total(nombre_ie, region, distrito)
    estados = clasificar_evidencias_por_estado(todas_las_filas, n_docentes_total)
    n_evidencias_validas = estados["n_validas"]

    pct_cobertura = (
        round(100 * n_evidencias_validas / n_docentes_total) if n_docentes_total > 0 else 0
    )
    muestra_suficiente = n_evidencias_validas >= UMBRAL_MUESTRA_INSUFICIENTE
    if not muestra_suficiente:
        log.info(
            "reporte_generado_muestra_insuficiente",
            cod_modular=cod_modular,
            n_evidencias_validas=n_evidencias_validas,
            pct_cobertura=pct_cobertura,
        )

    filas_ok = filas_validas(todas_las_filas)
    filas_por_docente = agrupar_por_docente(filas_ok)

    brechas_por_docente = {
        user_id: seleccionar_brechas_globales(filas_docente)
        for user_id, filas_docente in filas_por_docente.items()
    }

    distribucion = calcular_distribucion_por_criterio(filas_ok)
    distribucion_por_id = {c["criterio_id"]: c for c in distribucion}
    frecuencia_brechas = calcular_frecuencia_brechas(brechas_por_docente)

    fortalezas = clasificar_fortalezas(distribucion)
    necesidades_frecuentes = clasificar_necesidades_frecuentes(frecuencia_brechas)
    enfasis_lectura = calcular_enfasis_lectura(distribucion_por_id)
    aspectos_contraste = calcular_aspectos_contraste(distribucion, fortalezas)

    variables = {
        "cod_modular": cod_modular,
        "nombre_ie": nombre_ie,
        "n_docentes_total": n_docentes_total,
        "n_evidencias_validas": n_evidencias_validas,
        "n_evidencias_no_validas": estados["n_no_validas"],
        "n_sin_entrega": estados["n_sin_entrega"],
        "pct_cobertura": pct_cobertura,
        "muestra_suficiente": muestra_suficiente,
        "distribucion_por_aspecto": _enriquecer_distribucion_con_nombre(distribucion),
        "necesidades_frecuentes": _enriquecer_necesidades_con_nombre(necesidades_frecuentes),
        "fortalezas": _enriquecer_fortalezas_con_nombre(fortalezas),
        "retroalimentaciones": _extraer_retroalimentaciones(filas_por_docente),
    }
    narrativas = await redactar_secciones_narrativas(variables)
    preguntas_rtc = narrativas["preguntas_rtc"]

    # Adjunta a cada fortaleza/necesidad su manifestación pedagógica (el
    # fragmento que redactó el LLM), completando la plantilla del banco
    # de redacción (punto 12) -- las cifras las controla Python, el LLM
    # solo aporta el fragmento cualitativo.
    variables["fortalezas"] = [
        {**f, "manifestacion": _sin_punto_final(narrativas["manifestaciones"]["fortalezas"].get(f["id"], ""))}
        for f in variables["fortalezas"]
    ]
    variables["necesidades_frecuentes"] = [
        {**n, "manifestacion": _sin_punto_final(narrativas["manifestaciones"]["necesidades"].get(n["id"], ""))}
        for n in variables["necesidades_frecuentes"]
    ]

    # Hora local de Lima/Perú, explícita con offset -05:00 -- así el
    # frontend puede parsear un instante inequívoco sin depender de la
    # zona horaria del servidor ni de la del navegador de quien lo mire.
    ahora_lima = datetime.now(ZONA_LIMA)
    return {
        "reporte_id": f"{cod_modular}-{ahora_lima.strftime('%Y%m%dT%H%M%S')}",
        "cod_modular": cod_modular,
        "institucion_id": cod_modular,
        "nombre_ie": nombre_ie,
        "tipo_reporte": "REPORTE_INSTITUCIONAL_1",
        "hito_asociado": "HITO_1_DIRECTIVO",
        "version_evidencia": "V1",
        "fecha_generacion": ahora_lima.isoformat(),
        "n_docentes_total": n_docentes_total,
        "n_evidencias_validas": n_evidencias_validas,
        "n_evidencias_no_validas": estados["n_no_validas"],
        "n_sin_entrega": estados["n_sin_entrega"],
        "pct_cobertura": pct_cobertura,
        "muestra_suficiente": muestra_suficiente,
        "distribucion_por_aspecto": variables["distribucion_por_aspecto"],
        "necesidades_frecuentes": variables["necesidades_frecuentes"],
        "fortalezas": variables["fortalezas"],
        "enfasis_lectura": enfasis_lectura,
        "aspectos_contraste": aspectos_contraste,
        "preguntas_rtc": preguntas_rtc,
        "interpretaciones_por_aspecto": narrativas["interpretaciones"],
        "sintesis_institucional": narrativas["sintesis_institucional"],
        "identificacion_individual_incluida": False,
    }


# =============================================================================
# Caché
# =============================================================================

async def obtener_reporte_guardado(cod_modular: str) -> Optional[dict]:
    """Trae la fila de f4_reportes_directivo para esta IE, sin importar el
    estado (generado, muestra_insuficiente, sin_datos) -- o None si esta
    IE nunca tuvo ni un intento de generación."""
    query = """
        SELECT id, cod_modular, estado, n_docentes_evaluados, reporte_json,
               fecha_generacion, fecha_actualizacion
        FROM f4_reportes_directivo
        WHERE cod_modular = %(cod_modular)s
    """
    async with pool_cache.connection() as conn:
        return await (await conn.execute(query, {"cod_modular": cod_modular})).fetchone()


async def guardar_estado_reporte(
    cod_modular: str,
    estado: str,
    n_docentes_evaluados: int,
    reporte_json: Optional[dict] = None,
) -> int:
    """
    Upsert de la fila de f4_reportes_directivo para esta IE. estado es uno
    de 'generado', 'muestra_insuficiente', 'sin_datos'. reporte_json
    solo se pasa (y solo debe pasarse) cuando estado='generado'.

    Como hay una sola fila por cod_modular, esto cubre tanto el primer
    intento (exitoso o no) como los siguientes -- INSERT si es la
    primera vez que se toca esta IE, UPDATE si ya existía una fila
    (sin importar cuál era su estado anterior).
    """
    query = """
        INSERT INTO f4_reportes_directivo (cod_modular, estado, n_docentes_evaluados, reporte_json)
        VALUES (%(cod_modular)s, %(estado)s, %(n)s, %(reporte)s)
        ON CONFLICT (cod_modular) DO UPDATE SET
            estado = EXCLUDED.estado,
            n_docentes_evaluados = EXCLUDED.n_docentes_evaluados,
            reporte_json = COALESCE(EXCLUDED.reporte_json, f4_reportes_directivo.reporte_json),
            fecha_actualizacion = now()
        RETURNING id
    """
    async with pool_cache.connection() as conn:
        row = await (await conn.execute(query, {
            "cod_modular": cod_modular,
            "estado": estado,
            "n": n_docentes_evaluados,
            "reporte": Jsonb(reporte_json) if reporte_json is not None else None,
        })).fetchone()
        await conn.commit()

    return row["id"]


async def registrar_consulta(
    cod_modular: str, userid_directivo: int, courseid: int, cmid: int, id_reporte_directivo: int
) -> None:
    """Asocia un directivo con el reporte de su IE. Un registro único por
    (cod_modular, userid_directivo)"""
    query = """
        INSERT INTO f4_consultas_directivo
            (cod_modular, userid_directivo, courseid, cmid, id_reporte_directivo)
        VALUES (%(cod_modular)s, %(userid)s, %(courseid)s, %(cmid)s, %(id_reporte)s)
        ON CONFLICT (cod_modular, userid_directivo) DO NOTHING
    """
    async with pool_cache.connection() as conn:
        await conn.execute(query, {
            "cod_modular": cod_modular,
            "userid": userid_directivo,
            "courseid": courseid,
            "cmid": cmid,
            "id_reporte": id_reporte_directivo,
        })
        await conn.commit()


# =============================================================================
# Puntos de entrada
# =============================================================================

async def consultar_reporte_existente(userid: int) -> dict:
    """
    GET /reporte/consultar -- endpoint rápido de solo lectura, que además
    decide qué debe hacer el frontend a continuación, sin generar nada
    él mismo (nunca toca el LLM). Cuatro situaciones:

      1. No hay reporte, y ya hay >= UMBRAL_MUESTRA_INSUFICIENTE docentes
         con evidencia válida en la IE completa -> accion="generar" (el
         frontend llama a POST /reporte/generar automáticamente, sin
         botón visible).
      2. Hay reporte, y el número de docentes con evidencia válida no
         cambió desde que se generó -> accion="mostrar", reporte vigente
         tal cual.
      3. Hay reporte, pero el número de docentes con evidencia válida
         cambió -> accion="generar" (el frontend dispara la
         regeneración).
      4. No hay reporte, y aún no se llega al mínimo de docentes
         evaluados NI se cubrió el 100% de la plana docente de la IE
         completa -> accion="esperar_docentes". Si una IE ya evaluó a
         TODOS sus docentes (aunque sean menos de 5 en total), no tiene
         sentido esperar más -- se genera igual, marcado como
         muestra_suficiente=False en el reporte.

    Retorna siempre {"accion": ..., "reporte": ... | None, "n_docentes_evaluados": ...}
    """
    inicio = time.monotonic()
    log.info("reporte_consultado", userid=userid)

    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]

    guardado = await obtener_reporte_guardado(cod_modular)

    n_actual = await contar_docentes_evaluados(ie["nombre_ie"], ie["region"], ie["distrito"])
    n_total = await contar_docentes_total(ie["nombre_ie"], ie["region"], ie["distrito"])
    pct_cobertura_actual = round(100 * n_actual / n_total) if n_total > 0 else 0

    duracion_ms = round((time.monotonic() - inicio) * 1000)

    if guardado is None:
        debe_esperar = n_actual < UMBRAL_MUESTRA_INSUFICIENTE and pct_cobertura_actual < 100
        if debe_esperar:
            log.info(
                "consulta_esperar_docentes",
                cod_modular=cod_modular,
                n_docentes_evaluados=n_actual,
                pct_cobertura=pct_cobertura_actual,
                duracion_ms=duracion_ms,
            )
            return {"accion": "esperar_docentes", "reporte": None, "n_docentes_evaluados": n_actual}

        log.info(
            "consulta_debe_generar",
            cod_modular=cod_modular,
            motivo="sin_registro_previo",
            n_docentes_evaluados=n_actual,
            duracion_ms=duracion_ms,
        )
        return {"accion": "generar", "reporte": None, "n_docentes_evaluados": n_actual}

    if n_actual != guardado["n_docentes_evaluados"]:
        log.info(
            "consulta_debe_generar",
            cod_modular=cod_modular,
            motivo="nueva_actividad_docente",
            n_docentes_guardado=guardado["n_docentes_evaluados"],
            n_docentes_evaluados=n_actual,
            duracion_ms=duracion_ms,
        )
        return {"accion": "generar", "reporte": None, "n_docentes_evaluados": n_actual}

    log.info("consulta_mostrar_vigente", cod_modular=cod_modular, duracion_ms=duracion_ms)
    return {"accion": "mostrar", "reporte": guardado["reporte_json"], "n_docentes_evaluados": n_actual}


async def obtener_o_generar_reporte(userid: int, courseid: int, cmid: int) -> dict:
    """
    POST /reporte/generar -- pipeline completo:
      1. Resuelve cod_modular desde userid.
      2. Si no hay fila en reportes_directivo -> intenta generar.
      3. Si hay fila con estado='generado' -> compara n_docentes_evaluados
         actual vs guardado; si cambió, recalcula; si no, sirve el guardado.
      4. Si hay fila con estado distinto de 'generado' (intento previo
         fallido) -> reintenta generar con los datos actuales.
      5. Cualquier intento (éxito o falla) actualiza el estado guardado,
         para que el directivo sepa por qué no tiene informe todavía.
      6. Registra la asociación directivo-reporte solo si se logra
         generar (courseid/cmid: trazabilidad).
    """
    inicio = time.monotonic()
    log.info("reporte_solicitado", userid=userid, courseid=courseid, cmid=cmid)

    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]
    nombre_ie = ie["nombre_ie"]
    region = ie["region"]
    distrito = ie["distrito"]

    guardado = await obtener_reporte_guardado(cod_modular)

    debe_generar = (
        guardado is None
        or guardado["estado"] != "generado"
    )

    if guardado is not None and guardado["estado"] == "generado":
        filas_actuales = filas_validas(await obtener_filas_vista(nombre_ie, region, distrito))
        n_actual = len(agrupar_por_docente(filas_actuales))
        debe_generar = n_actual != guardado["n_docentes_evaluados"]
        if debe_generar:
            log.info(
                "cache_invalidado",
                cod_modular=cod_modular,
                n_docentes_guardado=guardado["n_docentes_evaluados"],
                n_docentes_actual=n_actual,
            )

    if not debe_generar:
        log.info("cache_hit", cod_modular=cod_modular, n_docentes=guardado["n_docentes_evaluados"])
        id_reporte = guardado["id"]
        reporte = guardado["reporte_json"]
    else:
        log.info("cache_miss_o_reintento", cod_modular=cod_modular)
        try:
            reporte = await generar_reporte(cod_modular, nombre_ie, region, distrito)
        except SinDatosDisponiblesError:
            await guardar_estado_reporte(cod_modular, "sin_datos", 0)
            raise
        id_reporte = await guardar_estado_reporte(
            cod_modular, "generado", reporte["n_evidencias_validas"], reporte
        )

    await registrar_consulta(cod_modular, userid, courseid, cmid, id_reporte)

    duracion_ms = round((time.monotonic() - inicio) * 1000)
    log.info("reporte_completado", userid=userid, cod_modular=cod_modular, duracion_ms=duracion_ms)

    return reporte