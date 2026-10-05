"""Climatología diaria 1991-2020 y calendario frente a lo normal."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import climatologia as c
import fetch_weather_dash as f


def serie_sintetica():
    """30 años con una máxima que sigue una onda anual (15 °C en enero,
    31 en julio) más ruido, mínima 10 °C por debajo y lluvia 1 de cada 5 días."""
    fechas = pd.date_range("1991-01-01", "2020-12-31", freq="D")
    rng = np.random.default_rng(0)
    onda = 23 - 8 * np.cos(2 * np.pi * (fechas.dayofyear - 15) / 365.25)
    tmax = onda + rng.normal(0, 2, len(fechas))
    return pd.DataFrame({"fecha": fechas, "tmax": tmax, "tmin": tmax - 10,
                         "prec": np.where(np.arange(len(fechas)) % 5 == 0, 4.0, 0.0)})


@pytest.fixture(scope="module")
def clima():
    return c.climatologia_diaria(serie_sintetica())


def test_tramos_cubren_la_serie_sin_huecos():
    lista = list(c.tramos())
    assert lista[0][0] == date(1991, 1, 1) and lista[-1][1] == date(2020, 12, 31)
    assert all(b[0] == a[1] + timedelta(days=1) for a, b in zip(lista, lista[1:], strict=False))
    assert all((fin - ini).days <= c.DIAS_TRAMO for ini, fin in lista)


def test_registros_de_aemet():
    filas = c.registros_a_filas([{"fecha": "1991-01-01", "tmax": "14,2", "tmin": "5,0", "prec": "Ip"},
                                 {"fecha": "1991-01-02", "tmax": "", "prec": "Acum"}])
    assert filas == [("1991-01-01", 14.2, 5.0, 0.0), ("1991-01-02", None, None, None)]


def test_normal_y_percentil_del_dia(clima):
    julio = c.comparar_dia(clima, "tmax", pd.Timestamp("2026-07-15"), 31.0)
    assert julio["normal"] == pytest.approx(31, abs=0.5) and 35 < julio["percentil"] < 65
    assert c.comparar_dia(clima, "tmax", pd.Timestamp("2026-01-15"), 31.0)["percentil"] == 100  # en enero, nunca
    assert c.comparar_dia(clima, "tmax", pd.Timestamp("2028-02-29"), None) is None
    assert c.comparar_dia({}, "tmax", pd.Timestamp("2026-07-15"), 31.0) is None


def test_descarga_reanudable(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CACHE_DIR", str(tmp_path))
    llamadas = []

    def pedir(inicio, fin):
        llamadas.append(inicio)
        if len(llamadas) == 3:
            raise RuntimeError("AEMET caído")
        return [{"fecha": f"{inicio}T00:00:00", "tmax": "20,0", "tmin": "10,0", "prec": "0,0"}]

    assert c.descargar_serie("X", pedir, pausa=0) is False
    assert len(c.leer_serie("X")) == 2
    llamadas.clear()
    assert c.descargar_serie("X", lambda i, f_: [], pausa=0) is True
    assert len(c.leer_serie("X")) == 2 and c._leer_tramos("X") == {i.isoformat() for i, _ in c.tramos()}


def test_calendario(clima):
    fin = pd.Timestamp("2026-07-19")  # domingo
    historico = pd.DataFrame({"fecha": pd.date_range("2025-07-01", "2026-07-17"), "tmax": 30.0, "tmin": 20.0, "prec": 0.0})
    historico.loc[historico["fecha"] == "2026-07-10", "tmax"] = 45.0
    provisional = pd.DataFrame({"fecha": pd.to_datetime(["2026-07-18"]), "tmax": [31.0], "tmin": [21.0], "prec": [12.0]})
    datos = f.datos_calendario(historico, provisional, fin=fin)
    assert datos["fecha"].iloc[0] == pd.Timestamp("2025-08-01") and datos["fecha"].iloc[-1] == fin  # 12 meses naturales
    assert datos.set_index("fecha").loc["2026-07-18", "provisional"]
    extremos = {"tmax": [{"valor": 44.0, "dia": 10.0, "anio": 2023.0, "unidad": "°C"}] * 12}
    normales = {("tmax", 35): 4.23, ("tmin", 20): 63.0}
    html = f.construir_calendario(datos, clima, extremos, normales)
    assert html.count('class="cal-vista"') == 3 and "hidden" in html
    assert "fuera de todo lo registrado" in html  # los 45 °C de julio
    assert "bate el récord de máxima de julio en la estación (44,0 °C, 10/07/2023)" in html
    assert "t6 u35 u40 record" in html  # la ★ sustituye al punto de «fuera»
    assert "prov" in html and "q3" in html  # 12 mm = clase 5-15
    assert "Días ≥ 35 °C: <strong>1</strong> · normal 4,2" in html  # solo los 45 °C pasan de 35
    assert "Noches tropicales (≥ 20 °C): <strong>" in html and "· normal 63" in html
    assert "Días ≥ 40 °C: <strong>1</strong></button>" in html  # sin normal: no se muestra
    # Heatmap mes × día, con «frente a lo normal» y «valor» en cada casilla.
    assert html.count('<span class="cal-mes">') == 3 * 12 and 'class="cal-modo-valor"' in html
    assert "--v:" in html and "solo-valor" in html and "solo-normal" in html
    assert 'class="cal-dia q3 r' in html  # los 12 mm: cantidad y frente a lo normal
    assert "aún no se ha descargado" in f.construir_calendario(datos, {})


def test_dias_por_anio():
    serie = serie_sintetica()
    assert c.dias_por_anio(serie, "prec", 1) == pytest.approx(365.25 / 5, rel=0.01)
    assert c.dias_por_anio(pd.DataFrame(), "tmax", 35) is None


def test_percentil_de_la_lluvia(clima):
    fecha = pd.Timestamp("2026-10-10")
    assert c.percentil_lluvia(clima, fecha, 0.4) is None  # no llovió (< 1 mm)
    poca, mucha = c.percentil_lluvia(clima, fecha, 1.0), c.percentil_lluvia(clima, fecha, 500.0)
    assert poca is not None and mucha == 100 and poca < mucha
    assert c.percentil_lluvia({}, fecha, 10.0) is None
