"""Cabeceras y resúmenes de las secciones (ahora, próximos días, histórico)."""
from datetime import datetime

import pandas as pd

import fetch_weather_dash as f


def _prediccion(probs, tmax=(28, 30, 31), tmin=(19, 20, 18)):
    hoy = pd.Timestamp(datetime.now(f.ZONA_HORARIA).date())
    return pd.DataFrame({"fecha": [hoy + pd.Timedelta(days=i) for i in range(3)],
                         "tmax": list(tmax), "tmin": list(tmin), "prob_precip": probs})


def test_resumen_pronostico():
    assert f.resumen_pronostico(_prediccion([0, 60, 90])) == "entre 18° y 31° · lluvia probable mañana (60 %)"
    assert f.resumen_pronostico(_prediccion([0, 0, 30])).endswith("alguna posibilidad de lluvia")
    assert f.resumen_pronostico(_prediccion([0, 5, 10])).endswith("sin lluvia a la vista")
    assert f.resumen_pronostico(pd.DataFrame()) == ""


def test_etiqueta_de_record_en_el_dia_previsto():
    ext = {"tmax": [{"valor": 30.5, "dia": 19, "anio": 1994, "unidad": "°C"}] * 12}
    html = f.construir_tarjetas_pronostico(_prediccion([0, 0, 0]), ext)
    assert html.count("etiqueta-record") == 2  # 28 no; 30 cerca; 31 supera
    assert "🏆 Récord de calor" in html and "Casi récord de calor" in html
    assert "etiqueta-record" not in f.construir_tarjetas_pronostico(_prediccion([0, 0, 0]))


def test_resumen_historico():
    fechas = pd.date_range("2025-10-01", "2026-09-19", freq="D")
    df = pd.DataFrame({"fecha": fechas, "tmed": 20.0, "prec": 1.0})
    normales = [{"mes": f"{m:02d}", "tm_mes_md": "19.0", "p_mes_md": "30.5"} for m in range(1, 13)]
    agosto = df[df["fecha"].dt.month == 8]
    texto = f.resumen_historico(df, (2026, 8, agosto), normales)
    assert texto.startswith("Agosto: +1,0 °C respecto a lo normal")
    assert "lluvia del año hidrológico:" in texto and texto.endswith("datos validados hasta el 19/09")
    assert f.resumen_historico(pd.DataFrame(), None, []) == ""


def test_cabecera_y_hora_de_observacion():
    assert f.hora_observacion({"fint": "2026-09-23T18:00:00+0000"}) == "20:00"
    assert f.hora_observacion(None) is None
    cabecera = f.cabecera_seccion("Ahora", "Medido por AEMET", sello="Observado a las 20:00")
    assert '<span class="texto-titulo">Ahora</span>' in cabecera and 'class="sello"' in cabecera
    assert "resumen-seccion" not in cabecera and cabecera.startswith('<details class="plegable-seccion" open>')


def test_tarjetas_ahora_agrupadas_en_aire_y_mar():
    html = f.construir_tarjetas_kpi({"ta": 25.0, "hr": 60}, mar={"ahora": 26.0, "hace_semana": None})
    assert html.index("grupo-aire") < html.index("grupo-mar")
    assert "Temperatura del agua" in html
    assert "grupo-mar" not in f.construir_tarjetas_kpi({"ta": 25.0, "hr": 60})


def test_barra_de_viento_en_la_prediccion():
    df = _prediccion([0, 0, 0])
    df["viento_max"] = [10, 30, None]
    df["racha_max"] = [None, 75, None]
    html = f.construir_tarjetas_pronostico(df)
    assert html.count("rango-viento") == 2  # el día sin viento no lleva barra
    assert "10 km/h" in html and "30–75 km/h" in html
    # Escala de 0 a 75 (la racha más fuerte): se llena desde 0 hasta lo más alto previsto.
    assert "left:0%;width:100%" in html  # racha de 75
    assert "left:0%;width:13%" in html  # sin racha: hasta el viento medio (10)
    assert f.color_viento(0) == "#D9F0A3" and f.color_viento(100) == "#543005"


def test_uv_en_las_tarjetas():
    df = _prediccion([0, 10, 20])
    df["uv_max"] = [8, 4, None]
    html = f.construir_tarjetas_pronostico(df)
    assert html.count("uv-tarjeta") == 2  # AEMET solo da el UV de los primeros días
    assert "background:#D8001D;color:#FFFFFF" in html and "UV 8" in html and "(muy alto)" in html
    assert "background:#F7E400;color:#1A1A1A" in html
    assert f.categoria_uv(11)[0] == "extremo"
