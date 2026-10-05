"""¿Cuánto aciertan?: archivo de previsiones y comparación con lo medido."""
import pandas as pd

import fetch_weather_dash as f

HOY = pd.Timestamp("2026-10-05")


def _pred(hoy, tmax=(25, 26, 27), prob=(0, 70, 10)):
    return pd.DataFrame({"fecha": [hoy + pd.Timedelta(days=i) for i in range(3)], "tmax": list(tmax),
                         "tmin": [17, 18, 19], "prob_precip": list(prob)})


def _modelos(hoy):
    filas = []
    for nombre in ("ECMWF", "ICON", "Météo-France", "GFS"):
        for i in range(3):
            filas.append({"fecha": hoy + pd.Timedelta(days=i), "modelo": nombre, "tmax": 26.0, "tmin": 18.0,
                          "prec": 4.0 if (nombre == "ECMWF" and i == 1) else 0.0})
    return pd.DataFrame(filas)


def test_previsiones_del_dia_y_archivo(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "CACHE_DIR", str(tmp_path))
    nuevas = f.previsiones_del_dia(_pred(HOY), _modelos(HOY), HOY)
    assert set(nuevas["antelacion"]) == {1, 2}  # el día de hoy no cuenta
    assert len(nuevas) == 5 * 2
    aemet = nuevas[(nuevas["fuente"] == "AEMET") & (nuevas["antelacion"] == 1)].iloc[0]
    assert aemet["lluvia"] == 70
    archivo = f.archivar_previsiones("X", nuevas, HOY)
    assert len(archivo) == 10
    # Más tarde, el mismo día: no se duplica ni se cambia la primera.
    otra = f.previsiones_del_dia(_pred(HOY, tmax=(30, 30, 30)), pd.DataFrame(), HOY)
    archivo = f.archivar_previsiones("X", otra, HOY)
    assert len(archivo) == 10 and archivo[archivo["fuente"] == "AEMET"]["tmax"].tolist() == [26, 27]
    # Al día siguiente se añaden las nuevas y se lee igual desde el CSV.
    manana = HOY + pd.Timedelta(days=1)
    f.archivar_previsiones("X", f.previsiones_del_dia(_pred(manana), pd.DataFrame(), manana), manana)
    leido = f.leer_previsiones("X")
    assert len(leido) == 12 and leido["fecha"].dtype.kind == "M"


def test_verificacion_y_bloque():
    ayer = HOY - pd.Timedelta(days=1)
    anteayer = HOY - pd.Timedelta(days=2)
    archivo = pd.concat([
        f.previsiones_del_dia(_pred(anteayer), _modelos(anteayer), anteayer),
        f.previsiones_del_dia(_pred(ayer - pd.Timedelta(days=1)), pd.DataFrame(), ayer - pd.Timedelta(days=1)),
    ], ignore_index=True).drop_duplicates(["emitida", "fuente", "fecha"])
    reales = f.dias_reales(pd.DataFrame({"fecha": [anteayer], "tmax": [27.4], "tmin": [16.2], "prec": [0.0]}),
                           pd.DataFrame({"fecha": [ayer], "tmax": [24.0], "tmin": [18.0], "prec": [6.0]}))
    verif = f.verificar_previsiones(archivo, reales, HOY)
    v = verif[(verif["fuente"] == "AEMET") & (verif["fecha"] == ayer) & (verif["antelacion"] == 1)].iloc[0]
    assert v["err_tmax"] == 2 and v["err_tmin"] == 0 and v["acierto_lluvia"]  # 70 % y llovió 6 mm
    ecmwf = verif[(verif["fuente"] == "ECMWF") & (verif["fecha"] == ayer)].iloc[0]
    assert ecmwf["acierto_lluvia"]  # 4 mm previstos, llovió
    icon = verif[(verif["fuente"] == "ICON") & (verif["fecha"] == ayer)].iloc[0]
    assert not icon["acierto_lluvia"]  # 0 mm previstos, llovió

    html = f.construir_bloque_acierto(verif, reales, HOY)
    assert html.index("AEMET") < html.index("ECMWF") < html.index("ICON") < html.index("Météo") < html.index("GFS")
    assert html.count('class="vista-plazo"') == 3 and html.count(" hidden") == 2
    assert "±2,0°" in html  # AEMET, máxima 1 día antes
    assert '<s class="e1"></s><s class="e0"></s><b class="ok">✓</b>' in html  # AEMET ayer: +2°, 0°, acertó la lluvia
    assert "<b>24°/18°</b><br>🌧️" in html


def test_bloque_sin_datos_todavia():
    texto = f.construir_bloque_acierto(pd.DataFrame(), pd.DataFrame(columns=["fecha"]), HOY, HOY)
    assert "Aún no hay días que comparar" in texto and "5 de octubre" in texto
