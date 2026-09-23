"""Cálculos meteorológicos y clasificaciones."""
from datetime import datetime
from unittest import mock

import pandas as pd
import pytest

import fetch_weather_dash as f


@pytest.mark.parametrize("valor, esperado", [
    (21.0, "Extremadamente frío"), (23.0, "Muy frío"), (23.4, "Frío"), (23.8, "Normal"),
    (24.2, "Cálido"), (25.0, "Muy cálido"), (26.0, "Extremadamente cálido"),
])
def test_clasificar_por_quintiles(valor, esperado):
    normal = {"tm_mes_min": "21.9", "tm_mes_q1": "23.1", "tm_mes_q2": "23.6", "tm_mes_q3": "23.9",
              "tm_mes_q4": "24.3", "tm_mes_max": "25.5"}
    assert f.clasificar(valor, normal, "tm_mes", f.CLASES_TEMPERATURA)[0] == esperado


def test_clasificar_sin_umbrales_devuelve_none():
    assert f.clasificar(20, {}, "tm_mes", f.CLASES_TEMPERATURA) is None


def test_sensacion_termica_y_punto_de_rocio():
    assert f.sensacion_termica(32, 60, 5) == pytest.approx(37.1, abs=0.3)   # tabla de índice de calor de la NOAA
    assert f.sensacion_termica(5, 70, 20) == pytest.approx(1.1, abs=0.3)    # sensación por viento
    assert f.sensacion_termica(20, 60, 10) == 20
    assert f.punto_de_rocio(26.3, 64) == pytest.approx(18.9, abs=0.2)
    assert f.confort_rocio(18.9) == "bochornoso"


def test_umbrales_de_intensidad_uv_y_viento():
    assert f.clase_intensidad_lluvia(1.2) == "débil"
    assert f.clase_intensidad_lluvia(30) == "fuerte"
    assert f.clase_intensidad_lluvia(154.8) == "torrencial"
    assert f.categoria_uv(8)[0] == "muy alto" and f.categoria_uv(11)[0] == "extremo"
    assert f._sector_viento(0) == "N" and f._sector_viento(112) == "E" and f._sector_viento(350) == "N"
    assert f._sector_viento(990) is None


def test_mes_completo_salta_meses_incompletos():
    fechas = pd.date_range("2026-08-01", "2026-09-18")
    df = pd.DataFrame({"fecha": fechas, "tmax": 30.0})

    class Fecha(datetime):
        @classmethod
        def utcnow(cls):
            return datetime(2026, 10, 3)

    with mock.patch.object(f, "datetime", Fecha):
        anio, mes, df_mes = f._mes_completo_mas_reciente(df)
    assert (anio, mes, len(df_mes)) == (2026, 8, 31)  # septiembre está incompleto


def test_precipitacion_normal_acumulada_completa_el_anio():
    normales = [{"mes": f"{m:02d}", "p_mes_md": "10"} for m in range(1, 13)]
    inicio = datetime(2025, 10, 1).date()
    fin = datetime(2026, 9, 30).date()
    assert f.precipitacion_normal_acumulada([fin], normales, inicio) == [pytest.approx(120)]


def test_rango_eje_solo_se_amplia_si_hace_falta():
    assert f._rango_eje([pd.Series([1, 2, 49])], 0, 50, 5) == [0, 50]
    assert f._rango_eje([pd.Series([0, 120.5])], 0, 50, 5) == [0, 125.5]
    assert f._rango_eje([None], 0, 80, 5) == [0, 80]


def test_numeros_en_formato_espanol():
    assert f._es(26.34) == "26,3"
    assert f._es(12, 0) == "12"


def test_notas_de_records():
    ext = {"tmax": [{"valor": 38.0, "dia": 19, "anio": 1994, "unidad": "°C"}] * 12}
    df = pd.DataFrame({"fecha": pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-26"]), "tmax": [37.4, 39.0, 30]})
    notas = f.notas_records(df, ext, prevision=True)
    assert len(notas) == 2
    assert "superaría el récord" in notas[0] and "39,0" in notas[0]
    assert "cerca del récord" in notas[1]
