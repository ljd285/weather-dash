"""Comparación de la predicción de AEMET con otros modelos (Open-Meteo)."""
import pandas as pd

import fetch_weather_dash as f

HOY = pd.Timestamp("2026-10-05")  # lunes


def _crudo():
    dias = [str((HOY + pd.Timedelta(days=i)).date()) for i in range(3)]
    diario = {"time": dias}
    datos = {
        "ecmwf_ifs025": ([29, 30, 25], [19, 20, 18], [0, 0, 12]),
        "icon_seamless": ([30, 31, 24], [20, 20, 19], [0, 0, 18]),
        "meteofrance_seamless": ([29, 30, 26], [19, 21, 19], [0, 0.5, 6]),
        "gfs_seamless": ([28, 29, 29], [19, 20, 20], [0, 0, 0.4]),
    }
    for codigo, (tmax, tmin, prec) in datos.items():
        diario[f"temperature_2m_max_{codigo}"] = tmax
        diario[f"temperature_2m_min_{codigo}"] = tmin
        diario[f"precipitation_sum_{codigo}"] = prec
    return {"daily": diario}


def _aemet():
    return pd.DataFrame({"fecha": [HOY + pd.Timedelta(days=i) for i in range(3)],
                         "tmax": [29, 30, 25], "tmin": [19, 20, 19], "prob_precip": [0, 5, 70]})


def test_modelos_a_dataframe():
    df = f.modelos_a_dataframe(_crudo())
    assert len(df) == 12 and set(df["modelo"]) == {"ECMWF", "ICON", "Météo-France", "GFS"}
    fila = df[(df["modelo"] == "ICON") & (df["fecha"] == HOY + pd.Timedelta(days=2))].iloc[0]
    assert (fila["tmax"], fila["tmin"], fila["prec"]) == (24, 19, 18)
    # Un modelo sin datos no aparece; una respuesta vacía da un DataFrame vacío.
    crudo = _crudo()
    crudo["daily"]["temperature_2m_max_gfs_seamless"] = [None] * 3
    crudo["daily"]["temperature_2m_min_gfs_seamless"] = [None] * 3
    crudo["daily"]["precipitation_sum_gfs_seamless"] = [None] * 3
    assert "GFS" not in set(f.modelos_a_dataframe(crudo)["modelo"])
    assert f.modelos_a_dataframe({}).empty


def test_acuerdo_y_resumen():
    acuerdo = f.acuerdo_modelos(_aemet(), f.modelos_a_dataframe(_crudo()))
    assert [d["nivel"] for d in acuerdo] == ["si", "si", "dudas"]
    assert acuerdo[2]["tmax"] == (24, 29) and acuerdo[2]["con_lluvia"] == [12, 18, 6]
    assert f.resumen_modelos(acuerdo, HOY) == (
        "Coinciden hoy y mañana. Hay dudas el miércoles: máxima entre 24° y 29°; 3 de 4 modelos dan lluvia (6–18 mm).")
    assert f.resumen_modelos(acuerdo[:2], HOY) == "Los modelos coinciden todos los días."
    assert f.acuerdo_modelos(_aemet(), pd.DataFrame()) == []


def test_tabla_y_graficos_de_modelos():
    df_modelos = f.modelos_a_dataframe(_crudo())
    acuerdo = f.acuerdo_modelos(_aemet(), df_modelos)
    html = f.construir_tabla_modelos(_aemet(), df_modelos, acuerdo, HOY)
    assert "<th>Hoy</th><th>Mañana</th><th>Mié 7</th>" in html
    assert html.count("<tr") == 1 + 5 + 1  # cabecera, AEMET y 4 modelos, acuerdo
    assert "70 %" in html and '<span class="chip q3">12 mm</span>' in html and "&lt; 1 mm" in html
    assert html.count("acuerdo-si") == 2 and html.count("acuerdo-dudas") == 1
    assert len(f.construir_graficos_modelos(_aemet(), df_modelos)) == 2


def test_la_minima_pide_mas_separacion_y_concordancia():
    df = f.modelos_a_dataframe(_crudo())
    aemet = _aemet()
    aemet.loc[0, "tmin"] = 15  # mínimas de 15 a 20 el lunes: 5° → alguna duda (en la máxima serían dudas)
    acuerdo = f.acuerdo_modelos(aemet, df)
    assert acuerdo[0]["nivel"] == "algo"
    assert "Alguna duda hoy: mínima entre 15° y 20°." in f.resumen_modelos(acuerdo, HOY)
    # Un solo modelo con lluvia: «da», no «dan».
    crudo = _crudo()
    for codigo in ("icon_seamless", "meteofrance_seamless"):
        crudo["daily"][f"precipitation_sum_{codigo}"] = [0, 0, 0]
    texto = f.resumen_modelos(f.acuerdo_modelos(_aemet(), f.modelos_a_dataframe(crudo)), HOY)
    assert "1 de 4 modelos da lluvia (12 mm)" in texto


def test_reintenta_si_open_meteo_no_responde(monkeypatch):
    llamadas = []

    class Respuesta:
        def raise_for_status(self):
            pass

        def json(self):
            return {"daily": {}}

    def get(url, params, timeout):
        llamadas.append(url)
        if len(llamadas) == 1:
            raise f.requests.exceptions.ReadTimeout("lento")
        return Respuesta()

    monkeypatch.setattr(f.requests, "get", get)
    monkeypatch.setattr(f.time, "sleep", lambda s: None)
    assert f.obtener_modelos(39.48, -0.37) == {"daily": {}}
    assert len(llamadas) == 2


def test_modelos_por_horas():
    horas = [str(pd.Timestamp("2026-10-19 12:00") + pd.Timedelta(hours=h)) for h in range(6)]
    crudo = {"hourly": {"time": horas}}
    for codigo in ("ecmwf_ifs025", "icon_seamless", "meteofrance_seamless", "gfs_seamless"):
        crudo["hourly"][f"temperature_2m_{codigo}"] = [20, 21, 22, 23, 22, 21]
        crudo["hourly"][f"precipitation_{codigo}"] = [0, 0, 2.5, 7, 0.2, 0]
    crudo["hourly"]["temperature_2m_meteofrance_seamless"][4:] = [None, None]  # fuera de su alcance
    crudo["hourly"]["precipitation_meteofrance_seamless"][4:] = [None, None]
    df = f.modelos_horas_a_dataframe(crudo)
    assert len(df) == 4 * 6 - 2 and set(df.columns) == {"hora", "modelo", "temp", "prec"}
    html = f.construir_graficos_horas_modelos(df)
    assert "Temperatura por horas" in html and "Lluvia por horas en cada modelo" in html
    assert 'class="lluvia-en-lineas"' in html and 'data-vista="lineas" hidden' in html
    assert html.index("ECMWF") < html.index("GFS") and "o-Fr." in html  # en el JSON de Plotly, con los acentos escapados
    assert f.construir_graficos_horas_modelos(pd.DataFrame()) == ""
    assert f.modelos_horas_a_dataframe({}).empty
