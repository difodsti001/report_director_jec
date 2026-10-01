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


def test_clasificar_fortalezas_usa_numerador_exacto_no_el_total():
    """El "n" de una fortaleza debe ser el conteo EXACTO de sesiones en
    Logrado+Destacado (el numerador de "[n] de [N]"), no el total de
    sesiones válidas del aspecto -- antes se usaba el total por error, lo
    que hacía que "n" fuera siempre igual a "N" (100%) sin importar el %
    real mostrado en la tabla."""
    # C1: 2 Inicio, 0 Desarrollo, 10 Logrado, 8 Destacado -> 20 sesiones,
    # 90% Logrado+Destacado (18 de 20, no 20 de 20).
    filas = (
        [_fila(i, 1, "inicio") for i in range(2)]
        + [_fila(i, 1, "logrado") for i in range(2, 12)]
        + [_fila(i, 1, "destacado") for i in range(12, 20)]
    )
    distribucion = core.calcular_distribucion_por_criterio(filas)
    fortalezas = core.clasificar_fortalezas(distribucion)

    assert len(fortalezas) == 1
    assert fortalezas[0]["pct"] == 90
    assert fortalezas[0]["n"] == 18  # 10 Logrado + 8 Destacado, no los 20 totales


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


def test_brechas_tienen_refiere_a_para_anclar_el_prompt():
    """Todas las brechas B1-B5 deben tener su propio texto 'refiere_a' --
    si se agrega una brecha nueva sin ese campo, el prompt quedaría sin
    anclaje semántico específico para su manifestación (ver
    _construir_prompt_secciones_narrativas)."""
    for brecha_id, info in core.BRECHAS.items():
        assert info.get("refiere_a"), f"{brecha_id} no tiene 'refiere_a'"


def test_prompt_ancla_manifestacion_de_necesidad_en_su_propio_aspecto():
    """Reproduce el bug reportado: la manifestación de una necesidad
    (ej. B4 'Mediación y evaluación formativa') no debe poder confundirse
    con la de otro aspecto (ej. B2, sobre demanda cognitiva) -- el prompt
    debe anclar cada necesidad con el 'refiere_a' de SU PROPIA brecha, no
    uno genérico ni copiable entre aspectos."""
    variables = {
        "cod_modular": "test",
        "nombre_ie": "IE de prueba",
        "n_docentes_total": 5,
        "n_evidencias_validas": 5,
        "pct_cobertura": 100,
        "muestra_suficiente": True,
        "distribucion_por_aspecto": [],
        "necesidades_frecuentes": [{"id": "B4", "nombre": "Mediación y evaluación formativa", "n": 2, "pct": 40}],
        "fortalezas": [],
        "retroalimentaciones": [],
    }
    prompt = core._construir_prompt_secciones_narrativas(variables)

    refiere_a_b4 = core.BRECHAS["B4"]["refiere_a"]
    refiere_a_b2 = core.BRECHAS["B2"]["refiere_a"]
    assert refiere_a_b4 in prompt
    assert refiere_a_b2 not in prompt


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


def test_seleccionar_brechas_globales_no_truena_con_criterion_index_none():
    """Fix de crash en producción: una fila 'success' con criterion_index
    NULL (docente a medio cargar en F2) no debe romper el ordenamiento
    por comparar None con int -- debe quedar al final del orden, no
    provocar un TypeError."""
    filas_docente = [
        {"criterion_index": None, "nivel_obtenido": "inicio", "brecha": "B1", "tipo_brecha": "Crítica"},
        {"criterion_index": 2, "nivel_obtenido": "inicio", "brecha": "B2", "tipo_brecha": "Crítica"},
    ]
    resultado = core.seleccionar_brechas_globales(filas_docente)
    assert set(resultado) == {"B1", "B2"}


def test_calcular_distribucion_descarta_fila_sin_criterion_index():
    """Fix de crash en producción: una fila 'success' con criterion_index
    NULL se descarta del cálculo (no se puede atribuir a ningún aspecto)
    en vez de romper la agrupación."""
    filas = [
        {"criterion_index": None, "nivel_obtenido": "logrado", "user_id": 99},
        {"criterion_index": 1, "nivel_obtenido": "logrado", "user_id": 1},
    ]
    resultado = core.calcular_distribucion_por_criterio(filas)
    assert len(resultado) == 1
    assert resultado[0]["criterio_id"] == "C1"
    assert resultado[0]["n"] == 1


def test_excluir_docentes_con_registro_incompleto():
    """Un docente con alguna fila 'success' sin criterion_index, nivel_
    obtenido o nivel_educativo se excluye COMPLETO -- no solo la fila
    rota -- para que sus otros aspectos ya cargados no generen un 'n de
    N' inconsistente entre secciones."""
    filas = [
        # Docente 1: completo, 2 filas success.
        _fila(1, 1, "logrado"),
        _fila(1, 2, "logrado"),
        # Docente 2: una fila incompleta (sin criterion_index) -- se
        # excluye TODO el docente, incluida su fila 1 que sí está completa.
        _fila(2, 1, "logrado"),
        {**_fila(2, 2, "logrado"), "criterion_index": None},
        # Docente 3: no válida, no debe verse afectado por la regla
        # (la regla solo aplica a filas 'success').
        _fila(3, 1, "inicio", status="validation_failed"),
    ]
    resultado = core._excluir_docentes_con_registro_incompleto(filas)
    user_ids_restantes = {f["user_id"] for f in resultado}
    assert user_ids_restantes == {1, 3}


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


def test_debe_esperar_mas_docentes_bajo_5_evidencias():
    """N=96 (banda B5), solo 4 evidencias válidas -> COB_CONFIDENCIAL
    (menos de 5 evidencias) -> debe esperar."""
    assert core._debe_esperar_mas_docentes(n_actual=4, n_total=96) is True


def test_debe_esperar_mas_docentes_bajo_umbral_ya_no_espera():
    """RE-04 de la Adenda: N=96, 5 evidencias válidas -> COB_BAJO_UMBRAL
    (no COB_CONFIDENCIAL) -> ya NO debe esperar, se emite con
    advertencia."""
    assert core._debe_esperar_mas_docentes(n_actual=5, n_total=96) is False


def test_debe_esperar_mas_docentes_cob_suficiente_no_espera():
    """N=40, 28 evidencias válidas -> COB_SUFICIENTE -> no debe esperar."""
    assert core._debe_esperar_mas_docentes(n_actual=28, n_total=40) is False


def test_debe_esperar_mas_docentes_banda_b0_escape_100_por_ciento():
    """IE muy pequeña (N=3, banda B0): nunca deja de ser COB_CONFIDENCIAL
    por más evidencias que junte (RE-05) -- pero si ya se evaluó al 100%
    de la plana docente, se genera igual (escape, mismo comportamiento
    que antes de la Adenda para IEs muy pequeñas)."""
    assert core._debe_esperar_mas_docentes(n_actual=2, n_total=3) is True
    assert core._debe_esperar_mas_docentes(n_actual=3, n_total=3) is False


def test_adenda_cp13_banda_b5_bajo_umbral():
    """CP13 (Adenda §14): N=96, 5 V1 válidas -> banda B5, umbral 58,
    COB_BAJO_UMBRAL."""
    resultado = core._clasificar_cobertura(n_evidencias_validas=5, n_docentes_total=96)
    assert resultado == {"banda_aplicada": "B5", "umbral_requerido": 58, "estado_cobertura": "COB_BAJO_UMBRAL"}


def test_adenda_cp14_banda_b3_suficiente():
    """CP14 (Adenda §14): N=40, 28 V1 válidas -> banda B3, umbral 28,
    COB_SUFICIENTE (justo en el umbral)."""
    resultado = core._clasificar_cobertura(n_evidencias_validas=28, n_docentes_total=40)
    assert resultado == {"banda_aplicada": "B3", "umbral_requerido": 28, "estado_cobertura": "COB_SUFICIENTE"}


def test_adenda_cp15_banda_b3_bajo_umbral_por_una_evidencia():
    """CP15 (Adenda §14): N=40, 27 V1 válidas (una menos que CP14) ->
    COB_BAJO_UMBRAL."""
    resultado = core._clasificar_cobertura(n_evidencias_validas=27, n_docentes_total=40)
    assert resultado["banda_aplicada"] == "B3"
    assert resultado["umbral_requerido"] == 28
    assert resultado["estado_cobertura"] == "COB_BAJO_UMBRAL"


def test_adenda_cp16_banda_b1_redondeo_hacia_arriba():
    """CP16 (Adenda §14): N=12, 10 V1 válidas -> banda B1, umbral
    ⌈0,85×12⌉=⌈10,2⌉=11 (redondeo siempre hacia arriba, RE-02) ->
    COB_BAJO_UMBRAL."""
    resultado = core._clasificar_cobertura(n_evidencias_validas=10, n_docentes_total=12)
    assert resultado == {"banda_aplicada": "B1", "umbral_requerido": 11, "estado_cobertura": "COB_BAJO_UMBRAL"}


def test_adenda_cp17_confidencial_por_menos_de_5_validas():
    """CP17 (Adenda §14): N=30, solo 4 V1 válidas -> COB_CONFIDENCIAL
    (RE-05), sin importar que N esté en banda B2."""
    resultado = core._clasificar_cobertura(n_evidencias_validas=4, n_docentes_total=30)
    assert resultado["estado_cobertura"] == "COB_CONFIDENCIAL"


def test_adenda_cp18_banda_b0_confidencial_aunque_cobertura_100():
    """CP18 (Adenda §14): N=4, 4 V1 válidas (100% de cobertura) -> banda
    B0, COB_CONFIDENCIAL de todas formas -- el umbral nunca aplica para
    N<5 (RE-05)."""
    resultado = core._clasificar_cobertura(n_evidencias_validas=4, n_docentes_total=4)
    assert resultado == {"banda_aplicada": "B0", "umbral_requerido": None, "estado_cobertura": "COB_CONFIDENCIAL"}


def test_construir_advertencia_cobertura_texto_exacto_de_la_adenda():
    """El texto debe coincidir con la Adenda Técnica, sección 8.1,
    variante RI 1 -- armado en Python, el LLM nunca lo toca."""
    texto = core._construir_advertencia_cobertura(
        n_evidencias_validas=5, umbral_requerido=58, n_docentes_total=96, pct_cobertura=5
    )
    assert texto == (
        "Este reporte se elaboró con 5 de las 58 sesiones "
        "válidas requeridas para representar a su institución (5 de "
        "96 docentes participantes; 5%). Los resultados describen "
        "únicamente las sesiones revisadas y no pueden generalizarse al conjunto de docentes. Por "
        "ello, los hallazgos de este reporte deben tratarse solo como hipótesis por contrastar con "
        "otras fuentes de su institución —MPE, ENLA, evidencias de aprendizaje de los estudiantes, "
        "registros de monitoreo y acompañamiento— antes de utilizarse en el Diagnóstico "
        "institucional y en la RTC 1."
    )


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
