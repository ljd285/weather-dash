"""Lectura y filtrado de los avisos CAP de AEMET."""
import io
import tarfile
from datetime import datetime, timedelta, timezone

import fetch_weather_dash as f

AHORA = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def cap(nivel, zona, evento, horas_inicio, horas_fin, tipo="Alert", codigo="774601"):
    inicio = (AHORA + timedelta(hours=horas_inicio)).isoformat()
    fin = (AHORA + timedelta(hours=horas_fin)).isoformat()
    return f"""<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><msgType>{tipo}</msgType>
<info><language>es-ES</language><event>{evento}</event><onset>{inicio}</onset><expires>{fin}</expires>
<description>Descripción</description>
<parameter><valueName>AEMET-Meteoalerta nivel</valueName><value>{nivel}</value></parameter>
<area><areaDesc>{zona}</areaDesc><geocode><valueName>AEMET-Meteoalerta zona</valueName><value>{codigo}</value></geocode></area></info>
<info><language>en-GB</language><event>English</event>
<parameter><valueName>AEMET-Meteoalerta nivel</valueName><value>{nivel}</value></parameter></info></alert>""".encode()


def empaquetar(*xmls):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for i, xml in enumerate(xmls):
            info = tarfile.TarInfo(f"{i}.xml")
            info.size = len(xml)
            tar.addfile(info, io.BytesIO(xml))
    return buffer.getvalue()


def test_lee_tar_y_filtra_por_zona_nivel_y_caducidad():
    contenido = empaquetar(
        cap("naranja", "Litoral norte de Valencia", "Aviso naranja por lluvias", -2, 10),
        cap("amarillo", "Litoral norte de Valencia", "Aviso caducado", -20, -1),
        cap("verde", "Litoral norte de Valencia", "Sin aviso", -1, 5),
        cap("rojo", "Litoral sur de Alicante", "Otra zona", -1, 5, codigo="770302"),
        cap("amarillo", "Litoral norte de Valencia", "Cancelado", -1, 5, tipo="Cancel"),
    )
    avisos = []
    for xml in f._ficheros_cap(contenido):
        avisos.extend(f._avisos_de_cap(f.ET.fromstring(xml)))
    assert len(avisos) == 3  # solo en español; sin "verde" ni cancelados
    zona = f.avisos_para_zona(avisos, "litoral norte de valencia", ahora=AHORA)
    assert [a["evento"] for a in zona] == ["Aviso naranja por lluvias"]
    assert f.avisos_para_zona(avisos, "774601", ahora=AHORA) == zona  # también por código


def test_xml_suelto_sin_tar():
    xml = cap("amarillo", "Litoral norte de Valencia", "Aviso", 1, 5)
    assert f._ficheros_cap(xml) == [xml]


def test_banner_sin_avisos_y_con_error():
    assert "Sin avisos" in f.construir_banner_avisos([], "Litoral norte de Valencia")
    assert "No se pudieron consultar" in f.construir_banner_avisos([], "Zona", error=True)


def test_banner_con_avisos_toma_el_color_del_peor_nivel():
    aviso = {"nivel": "amarillo", "evento": "Aviso amarillo por lluvias", "titular": "", "descripcion": "40 mm en 1 h",
             "inicio": None, "fin": None, "zonas": []}
    banner = f.construir_banner_avisos([aviso, dict(aviso, nivel="naranja", evento="Aviso naranja por tormentas")], "Zona")
    assert "avisos-naranja" in banner and "2 avisos meteorológicos" in banner
    assert banner.count('class="aviso-meteo') == 2
