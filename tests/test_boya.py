"""Datos de la boya de Puertos del Estado (formato real de Portus)."""
from datetime import datetime

import pandas as pd

import fetch_weather_dash as f

# Tres filas copiadas de una respuesta real (Boya de Valencia, 22/09/2026),
# más una con un dato de calidad dudosa (2) y otra con un hueco ("-").
CRUDO = [
    ["UTC", "Hm0 (m)", "Tp (s)", "MeanDir (º)", "WaterTemp (ºC)"],
    [
        [1790107200, [0.59, 1], [4.1, 1], [142.0, 1], [24.93, 1]],
        [1790110800, [0.7, 1], [4.3, 1], [143.0, 1], [24.84, 1]],
        [1790114400, [0.7, 1], [4.49, 1], [139.0, 1], [24.81, 1]],
        [1790118000, [3.5, 2], [4.49, 1], [139.0, 1], [24.79, 1]],
        [1790121600, "-", [4.3, 1], [57.0, 1], [24.87, 1]],
    ],
]


def test_boya_a_dataframe_convierte_fechas_y_filtra_calidad():
    df = f.boya_a_dataframe(CRUDO)
    assert list(df.columns) == ["fecha", "Hm0", "Tp", "MeanDir", "WaterTemp"]
    assert df["fecha"].iloc[0] == pd.Timestamp("2026-09-22 20:00")  # segundos UTC
    assert df["WaterTemp"].tolist()[:2] == [24.93, 24.84]
    assert pd.isna(df.loc[3, "Hm0"])  # calidad 2: se descarta
    assert pd.isna(df.loc[4, "Hm0"])  # hueco


def test_formato_inesperado_da_error_claro():
    try:
        f.boya_a_dataframe({"error": "x"})
    except ValueError as exc:
        assert "formato inesperado" in str(exc)
    else:
        raise AssertionError("debería fallar")


def test_resumen_toma_la_ultima_lectura_valida_de_cada_variable():
    resumen = f.resumen_boya(f.boya_a_dataframe(CRUDO), ahora=datetime(2026, 9, 23, 0, 30))
    assert resumen["WaterTemp"] == 24.87
    assert resumen["Hm0"] == 0.7  # la de las 22:00; las dos siguientes no valen
    assert resumen["MeanDir"] == 57.0
    assert "WaterTemp_24h" not in resumen  # no hay datos de 24 h antes


def test_resumen_ignora_datos_antiguos():
    assert f.resumen_boya(f.boya_a_dataframe(CRUDO), ahora=datetime(2026, 9, 23, 12, 0)) is None


def test_tarjetas_con_boya_y_con_reserva_del_modelo():
    resumen = f.resumen_boya(f.boya_a_dataframe(CRUDO), ahora=datetime(2026, 9, 23, 0, 30))
    html = f.construir_tarjetas_kpi(None, mar={"ahora": 26.0, "hace_semana": None}, boya=resumen, nombre_boya="Boya de Valencia")
    assert "24,9 °C" in html and "26,0" not in html  # manda la boya
    assert "Oleaje (altura significativa)" in html and "0,7 m" in html and "del NE" in html
    assert "Boya de Valencia (Puertos del Estado)" in html
    sin_temperatura = {k: v for k, v in resumen.items() if not k.startswith("WaterTemp")}
    html = f.construir_tarjetas_kpi(None, mar={"ahora": 26.0, "hace_semana": None}, boya=sin_temperatura)
    assert "26,0 °C" in html and "modelo de Copernicus" in html


def test_graficos_de_la_boya():
    graficos = f.construir_graficos_boya(f.boya_a_dataframe(CRUDO), "Boya de Valencia")
    assert len(graficos) == 2
    assert 'aria-label="Temperatura del agua"' in graficos[0]
    assert f.construir_graficos_boya(pd.DataFrame(), "x") == []
