"""Acumulación de lecturas horarias, días provisionales y minigráfico."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import fetch_weather_dash as f


def lecturas(desde, horas, ta=20.0, prec=0.0):
    return [{"fint": (desde + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S"), "ta": ta + h % 24 / 4,
             "prec": prec, "vmax": 5.0} for h in range(horas)]


def test_acumular_sin_duplicados_y_con_caducidad():
    viejas = lecturas(datetime(2026, 9, 1), 24)
    nuevas = lecturas(datetime(2026, 9, 12, 12), 30)
    acumuladas = f.acumular_observaciones(viejas + nuevas[:5], nuevas, ahora=datetime(2026, 9, 14))
    assert len(acumuladas) == 30  # las del día 1 caducan (más de 10 días) y no hay horas repetidas
    assert acumuladas == sorted(acumuladas, key=lambda r: r["fint"])


def test_dias_provisionales_exigen_dias_completos():
    obs = lecturas(datetime(2026, 9, 19, 1), 24 * 3 + 5, prec=0.5)  # del 19 01:00 al 22 05:00 UTC
    df = f.dias_provisionales(obs, pd.Timestamp("2026-09-18"))
    assert list(df["fecha"].dt.day) == [19, 20, 21]  # el 22 no ha terminado
    fila = df.iloc[0]
    assert fila["tmax"] == pytest.approx(25.75) and fila["tmin"] == pytest.approx(20.0)
    assert fila["prec"] == pytest.approx(12.0)  # 24 lecturas de 0,5 mm entre las 07 y las 07 UTC
    assert fila["racha"] == pytest.approx(18.0)  # 5 m/s -> km/h
    assert pd.isna(df.iloc[-1]["prec"])  # la lluvia del 21 acaba el 22 a las 07 y aún faltan lecturas


def test_dias_provisionales_descarta_dias_con_huecos():
    obs = lecturas(datetime(2026, 9, 19, 1), 10) + lecturas(datetime(2026, 9, 20, 1), 30)
    df = f.dias_provisionales(obs, pd.Timestamp("2026-09-18"))
    assert list(df["fecha"].dt.day) == [20]


def test_minigrafico_y_cambio_en_24_horas():
    obs = lecturas(datetime(2026, 9, 20), 26)
    svg, cambio = f.construir_minigrafico(obs)
    assert svg.startswith("<svg") and "polyline" in svg
    assert cambio == "+0,0 °C que hace 24 h"  # la serie de prueba se repite cada 24 h
    assert f.construir_minigrafico(obs[:2]) == ("", "")


@pytest.mark.parametrize("sufijo", ["+0000", "+00:00", "Z", ""])
def test_horas_de_aemet_con_o_sin_zona_horaria(sufijo):
    """AEMET da "fint" con zona ("2026-09-19T01:00:00+0000"); no debe romper nada."""
    obs = [dict(r, fint=r["fint"] + sufijo) for r in lecturas(datetime(2026, 9, 19, 1), 24 * 3 + 5, prec=0.5)]
    df = f.dias_provisionales(obs, pd.Timestamp("2026-09-18"))
    assert list(df["fecha"].dt.day) == [19, 20, 21]
    svg, cambio = f.construir_minigrafico(obs)
    assert svg and cambio
    acumuladas = f.acumular_observaciones(obs[:10], [dict(obs[5], fint=obs[5]["fint"].replace(sufijo, "") or obs[5]["fint"])],
                                          ahora=datetime(2026, 9, 22, 12))
    assert len(acumuladas) == 10  # la misma hora con otro formato no se duplica


@pytest.mark.parametrize("fint", ["2026-09-23T18:00:00+0000", "2026-09-23T18:00:00"])
def test_tarjeta_ahora_con_hora_de_aemet(fint):
    html = f.construir_tarjetas_kpi({"fint": fint, "ta": 25.0, "hr": 60})
    assert "Última observación: 23/09 a las 20:00 (hora local)" in html  # 18 UTC = 20 h en verano


def test_lluvia_de_hoy_y_de_ayer():
    # Lecturas horarias (UTC) del 27 y el 28; en verano, hora local = UTC+2.
    # Cada lectura trae la lluvia de la hora anterior.
    lluvia = {(7, 21), (8, 22), (8, 23)}
    obs = [{"fint": f"2026-09-2{d}T{h:02d}:00:00+0000", "prec": 1.0 if (d, h) in lluvia else 0.0}
           for d in (7, 8) for h in range(24)]
    ahora = datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc)  # 29/09 a la 01:30 local
    acumulados = f.lluvia_por_dia(obs, ahora=ahora)
    assert acumulados["hoy"] == (1.0, 1)  # 23 UTC del 28 = de 00 a 01 local del 29
    assert acumulados["ayer"] == (1.0, 24)  # 22 UTC del 28 = de 23 a 24 local del 28; la del 27 no cuenta
    assert f.lluvia_por_dia([]) == {}


def test_tarjeta_de_lluvia_con_el_acumulado_de_hoy():
    ahora = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    obs = [{"fint": (ahora - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "ta": 20.0, "prec": 0.0}
           for h in range(48, -1, -1)]
    obs[-1]["prec"] = 2.4
    html = f.construir_tarjetas_kpi(obs[-1], observaciones=obs)
    assert "<h3>Lluvia hoy</h3>" in html and "última hora 2,4 mm" in html and "ayer 0,0 mm" in html
    assert "<h3>Lluvia (última hora)</h3>" in f.construir_tarjetas_kpi({"prec": 0.0})  # sin lecturas horarias
