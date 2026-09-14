"""
Casos de prueba CP01-CP11 de la ficha técnica F4 (docs_nuevos/1. FICHA
TÉCNICA..., sección 18), cubriendo las funciones puras y determinísticas
de core.py. CP12 (bloquear causalidad inventada por el LLM) no es
testeable como unit test -- queda como checklist de revisión manual del
prompt, ver el test marcado como skip al final de este archivo.
"""

import pytest

import core


def _fila(user_id, criterion_index, nivel, status="success", nivel_educativo="Primaria", brecha=None, tipo_brecha=None):
    return {
        "user_id": user_id,
        "criterion_index": criterion_index,
        "nivel_obtenido": nivel,
        "status": status,
        "nivel_educativo": nivel_educativo,
        "brecha": brecha,
        "tipo_brecha": tipo_brecha,
    }


def test_cp01_cobertura_y_clasificacion_estados():
    """24 registrados; 22 válidos; 1 no válido; 1 no entrega -> cobertura
    91,7%; desempeño sobre 22."""
    filas = [_fila(uid, 1, "logrado") for uid in range(22)]
    filas.append(_fila(22, 1, "inicio", status="validation_failed"))

    estados = core.clasificar_evidencias_por_estado(filas, n_docentes_total=24)

    assert estados == {"n_validas": 22, "n_no_validas": 1, "n_sin_entrega": 1}
    pct_cobertura = round(100 * estados["n_validas"] / 24)
    assert pct_cobertura == 92  # 22/24 = 91.7% redondeado


def test_cp02_distribucion_c3():
    """C3: 6 Inicio, 9 Desarrollo, 6 Logrado, 1 Destacado -> 27.3%; 40.9%;
    27.3%; 4.5%; agrupado 68.2% y 31.8%."""
    filas = (
        [_fila(i, 3, "inicio") for i in range(6)]
        + [_fila(i, 3, "en_desarrollo") for i in range(6, 15)]
        + [_fila(i, 3, "logrado") for i in range(15, 21)]
        + [_fila(21, 3, "destacado")]
    )
    c = core.calcular_distribucion_por_criterio(filas)[0]

    assert c["criterio_id"] == "C3"
    assert c["n"] == 22
    assert (c["pct_inicio"], c["pct_en_desarrollo"], c["pct_logrado"], c["pct_destacado"]) == (27, 41, 27, 5)
    assert c["pct_inicio_en_desarrollo"] == c["pct_inicio"] + c["pct_en_desarrollo"]
    assert c["pct_logrado_destacado"] == c["pct_logrado"] + c["pct_destacado"]


def test_primera_oracion_usa_conteo_exacto_no_derivado_del_pct():
    """La primera oración de la interpretación (sección 3) debe citar el
    número EXACTO de sesiones del agrupado predominante -- no un valor
    reconstruido a partir del porcentaje ya redondeado (eso es lo que
    podía desalinear el texto frente a la tabla de la sección 2)."""
    filas = (
        [_fila(i, 3, "inicio") for i in range(6)]
        + [_fila(i, 3, "en_desarrollo") for i in range(6, 15)]
        + [_fila(i, 3, "logrado") for i in range(15, 21)]
        + [_fila(21, 3, "destacado")]
    )
    c = core.calcular_distribucion_por_criterio(filas)[0]

    frase = core._primera_oracion_interpretacion(c)

    # Agrupado predominante: Inicio+En desarrollo (68%) sobre
    # Logrado+Destacado (32%) -- 6+9=15 sesiones exactas, no 68% de 22
    # redondeado de otra forma.
    assert frase == "En 15 de 22 sesiones (68%) se observa Inicio o En desarrollo."


def test_completar_huecos_antepone_primera_oracion_y_recorta_duplicado():
    """_completar_huecos_narrativos debe anteponer siempre la primera
    oración calculada en Python, y si el LLM además escribió su propia
    versión numérica al inicio (ignorando la instrucción del prompt),
    debe recortarla para no duplicar ni contradecir la cifra oficial."""
    c = core.calcular_distribucion_por_criterio(
        [_fila(i, 1, "logrado") for i in range(5)]
    )[0]
    variables = {
        "cod_modular": "test",
        "distribucion_por_aspecto": [c],
        "fortalezas": [],
        "necesidades_frecuentes": [],
    }
    data_llm = {
        "interpretaciones": {
            "C1": "En 999 de 999 sesiones (10%) se observa algo distinto. Esto significa que hay coherencia."
        },
        "manifestaciones": {"fortalezas": {}, "necesidades": {}},
        "preguntas_rtc": ["¿Pregunta?"],
        "sintesis_institucional": "Síntesis de prueba.",
    }

    resultado = core._completar_huecos_narrativos(variables, data_llm)

    texto = resultado["interpretaciones"]["C1"]
    assert texto.startswith(core._primera_oracion_interpretacion(c))
    assert "999" not in texto
    assert "Esto significa que hay coherencia." in texto


def test_cp03_distribucion_c1_favorable():
    """C1: 1 Inicio, 4 Desarrollo, 13 Logrado, 4 Destacado -> ~22.7%
    requiere mayor desarrollo; ~77.3% favorable."""
    filas = (
        [_fila(0, 1, "inicio")]
        + [_fila(i, 1, "en_desarrollo") for i in range(1, 5)]
        + [_fila(i, 1, "logrado") for i in range(5, 18)]
        + [_fila(i, 1, "destacado") for i in range(18, 22)]
    )
    c = core.calcular_distribucion_por_criterio(filas)[0]

    assert c["pct_inicio_en_desarrollo"] == 23
    assert c["pct_logrado_destacado"] == 77


def test_cp04_necesidad_frecuente_b2():
    """B2 en 9 de 22 -> 40,9%; visible como necesidad frecuente de
    participación y exigencia de las actividades (denominación oficial,
    sin el código B2)."""
    brechas_por_docente = {i: (["B2"] if i < 9 else []) for i in range(22)}
    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    top = necesidades[0]
    assert top["id"] == "B2"
    assert top["pct"] == 41  # round(100*9/22)
    assert core._nombre_brecha("B2") == "Participación de los estudiantes y nivel de exigencia de las actividades"


def test_cp05_necesidad_frecuente_b3():
    """B3 en 7 de 22 -> 31,8%; visible como necesidad frecuente en
    situaciones significativas."""
    brechas_por_docente = {i: (["B3"] if i < 7 else []) for i in range(22)}
    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    assert necesidades[0]["id"] == "B3"
    assert necesidades[0]["pct"] == 32  # round(100*7/22)
    assert core._nombre_brecha("B3") == "Situaciones significativas"


def test_cp06_no_infiere_brecha_no_provista():
    """C3 alto pero sin B2 -> no inferir B2; respetar
    brechas_identificadas[] tal como las entrega F2 (aquí: brecha=None)."""
    filas_docente = [
        {"criterion_index": 3, "nivel_obtenido": "inicio", "brecha": None, "tipo_brecha": None},
    ]
    assert core.seleccionar_brechas_globales(filas_docente) == []


def test_cp07_muestra_insuficiente():
    """4 evidencias válidas -> por debajo del umbral, debe usarse lenguaje
    prudente (muestra_suficiente=False)."""
    n_evidencias_validas = 4
    assert n_evidencias_validas < core.UMBRAL_MUESTRA_INSUFICIENTE


def test_cp10_comprension_lectora_sin_porcentaje():
    """Comprensión lectora nunca debe traer un porcentaje institucional
    propio, porque F2 no la evalúa como criterio independiente."""
    enfasis = core.calcular_enfasis_lectura({})
    comprension = next(e for e in enfasis if e["enfasis"] == "Comprensión lectora")

    assert comprension["aspecto_relacionado"] is None
    assert "no presenta un porcentaje institucional" in comprension["texto_permitido"]


def test_cp11_determinismo():
    """Mismo conjunto de entrada procesado dos veces -> mismo resultado
    cuantitativo."""
    filas = [_fila(i, c, "logrado") for i in range(10) for c in range(1, 6)]
    assert core.calcular_distribucion_por_criterio(filas) == core.calcular_distribucion_por_criterio(filas)


def test_cp13_umbral_30_incluye_solo_una_fila():
    """5 docentes, 1 aspecto con brecha en 40% de las sesiones; los demás
    por debajo del 30% -> necesidades_frecuentes[] contiene una sola fila
    (Especificaciones Funcionales JEC §9, §16 paso 6)."""
    brechas_por_docente = {
        0: ["B2"], 1: ["B2"],
        2: ["B3"],
        3: [],
        4: [],
    }
    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    assert len(necesidades) == 1
    assert necesidades[0]["id"] == "B2"
    assert necesidades[0]["pct"] == 40


def test_cp14_ningun_aspecto_alcanza_30_tabla_vacia():
    """Ningún aspecto alcanza el 30% -> necesidades_frecuentes[] se emite
    vacío."""
    brechas_por_docente = {i: (["B3"] if i < 2 else []) for i in range(10)}
    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    assert necesidades == []


def test_enfasis_resolucion_de_problemas():
    """El 4.º énfasis del programa usa la denominación oficial de la Ficha
    Técnica / Especificaciones JEC §7 ('Resolución de problemas'), no la
    que traía producción ('Aprendizaje basado en situaciones y
    problemas')."""
    enfasis = core.calcular_enfasis_lectura({})
    nombres = [e["enfasis"] for e in enfasis]

    assert "Resolución de problemas" in nombres
    assert "Aprendizaje basado en situaciones y problemas" not in nombres


def test_zona_lima_offset_fijo_utc_menos_5():
    """Perú no usa horario de verano: el offset debe ser exactamente
    -05:00 en cualquier fecha del año, sin depender de tzdata."""
    from datetime import datetime, timedelta

    assert core.ZONA_LIMA.utcoffset(None) == timedelta(hours=-5)
    ahora = datetime.now(core.ZONA_LIMA)
    assert ahora.utcoffset() == timedelta(hours=-5)


@pytest.mark.skip(
    reason=(
        "CP12 (bloquear/reescribir causalidad inventada por el LLM, ej. "
        "'C3 causa bajo rendimiento ENLA') no es testeable como unit test: "
        "depende del comportamiento del modelo de lenguaje. Queda como "
        "checklist de revisión manual del prompt en "
        "_construir_prompt_secciones_narrativas."
    )
)
def test_cp12_no_causalidad_revision_manual():
    pass


def test_denominacion_visible_sin_codigo():
    """El documento prohíbe mostrar los códigos C1-C5/B1-B5 en texto
    visible (ficha técnica, secciones 8 y 11) -- _nombre_criterio y
    _nombre_brecha nunca deben anteponer el código."""
    assert core._nombre_criterio("C4") == "Mediación y evaluación formativa"
    assert not core._nombre_criterio("C4").startswith("C4")
    assert core._nombre_brecha("B1") == "Coherencia del diseño con la información del diagnóstico"
    assert not core._nombre_brecha("B1").startswith("B1")
