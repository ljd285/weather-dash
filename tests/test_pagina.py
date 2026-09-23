"""La página se genera aunque fallen todas las fuentes de datos."""
import fetch_weather_dash as f


def test_pagina_se_genera_con_todo_caido(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AEMET_API_KEY", "prueba")

    def sin_datos(*args, **kwargs):
        raise f.SinDatosAEMET("sin datos")

    def sin_red(*args, **kwargs):
        raise OSError("sin red")

    monkeypatch.setattr(f, "aemet_get", sin_datos)
    monkeypatch.setattr(f.requests, "get", sin_red)
    monkeypatch.setattr(f.time, "sleep", lambda s: None)

    f.main()

    pagina = (tmp_path / "docs" / "index.html").read_text(encoding="utf-8")
    assert pagina.startswith("<!DOCTYPE html>")
    assert "${" not in pagina  # no queda ningún hueco de plantilla sin rellenar
    assert "Valencia" in pagina
    assert "No se pudieron cargar datos históricos" in pagina
