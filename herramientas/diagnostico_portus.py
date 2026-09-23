"""
Diagnóstico (temporal) del servicio de datos de Portus (Puertos del Estado)
para la Boya de Valencia. No genera nada: solo escribe en el log qué
responde Portus, para decidir cómo integrar sus datos en el dashboard.

1. Condiciones de acceso automático (robots.txt).
2. La página de la gráfica en tiempo real de la boya y los scripts que
   carga: se buscan en ellos las direcciones de datos que llama la web.
3. Se prueban esas direcciones (y algunas candidatas) y se resume cada
   respuesta: código, tipo, tamaño y estructura si es JSON.
"""

import json
import re
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://portus.puertos.es"
ESTACION = 2630  # Boya de Valencia (red de aguas profundas, REDEXT)
PAGINAS = [
    f"{BASE}/PortusData/rtChart?station={ESTACION}&params=WaterTemp%2CWaterTemp&dirParams=&int=default&isRadar=false&locale=es",
    f"{BASE}/Portus_RT/chart.html?c=4&p=MjYzMDs7bnVsbDs7Mzg7Oy0xOztUZW1wZXJhdHVyYSBkZWwgYWd1YQ%3D%3D&locale=es",
    f"{BASE}/static/PortusDashboard/?code={ESTACION}&locale=es",
]
CANDIDATAS = [
    f"{BASE}/portussvr/api/RTData/station/{ESTACION}?locale=es",
    f"{BASE}/portussvr/api/lastData/station/{ESTACION}?locale=es",
    f"{BASE}/portussvr/api/station/{ESTACION}?locale=es",
    f"{BASE}/PortusData/api/rtData?station={ESTACION}",
]
SESION = requests.Session()
SESION.headers["User-Agent"] = "weather-dash (dashboard personal; github.com/ljd285/weather-dash)"
PATRON_RUTA = re.compile(r"""["'`]((?:https?://[^"'`\s]*)?/?(?:portussvr|PortusData|api|rest|ws)/[^"'`\s<>]{2,200})["'`]""")


def resumir(url, max_texto=600):
    try:
        r = SESION.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"  ERROR {url}: {exc}")
        return None
    tipo = r.headers.get("content-type", "")
    print(f"  {r.status_code} {tipo} {len(r.content)} bytes  {url}")
    if "json" in tipo or r.text.lstrip()[:1] in "[{":
        try:
            datos = r.json()
            print("  JSON:", describir(datos))
            print("  Muestra:", json.dumps(datos, ensure_ascii=False)[:max_texto])
            return r
        except ValueError:
            pass
    print("  Texto:", re.sub(r"\s+", " ", r.text[:max_texto]))
    return r


def describir(datos, nivel=0):
    if isinstance(datos, dict):
        if nivel > 2:
            return "{...}"
        return "{" + ", ".join(f"{k}: {describir(v, nivel + 1)}" for k, v in list(datos.items())[:25]) + "}"
    if isinstance(datos, list):
        return f"[{len(datos)} × {describir(datos[0], nivel + 1) if datos else 'vacía'}]"
    return type(datos).__name__


def rutas_en(texto):
    return sorted({m.group(1) for m in PATRON_RUTA.finditer(texto)})


def llamadas_en_scripts():
    """Busca en los scripts de la gráfica en tiempo real cómo piden los datos
    (ajax/fetch/d3.json/POST) y muestra el código de alrededor."""
    base = f"{BASE}/PortusData/"
    for nombre in ["js/main/rtChart.js", "js/common/commonChart.js", "js/common/interfazDefault.js",
                   "js/common/controlesDefault.js"]:
        url = urljoin(base, nombre)
        try:
            js = SESION.get(url, timeout=30).text
        except requests.RequestException as exc:
            print(f"  ERROR {url}: {exc}")
            continue
        print(f"\n== {nombre} ({len(js)} caracteres)")
        patron = re.compile(r"(?i)(\$\.ajax|\$\.post|\$\.getJSON|d3\.json|d3\.request|fetch\(|XMLHttpRequest|portussvr|RTData|lastData|contentType|type\s*:\s*['\"]POST)")
        vistos = 0
        for m in patron.finditer(js):
            inicio = max(0, m.start() - 250)
            print("  ···", re.sub(r"\s+", " ", js[inicio:m.start() + 450]))
            vistos += 1
            if vistos >= 12:
                break


def pruebas_post():
    """Los servicios RTData/lastData responden 405 a GET: se prueban con POST."""
    for ruta in ["RTData/station", "lastData/station"]:
        url = f"{BASE}/portussvr/api/{ruta}/{ESTACION}?locale=es"
        for cuerpo in [["WaterTemp"], ["WaterTemp", "Hm0", "Tp", "MeanDir"], {}, None]:
            try:
                r = SESION.post(url, json=cuerpo, timeout=30) if cuerpo is not None else SESION.post(url, timeout=30)
            except requests.RequestException as exc:
                print(f"  ERROR POST {url}: {exc}")
                continue
            print(f"  POST {url} cuerpo={json.dumps(cuerpo)} -> {r.status_code} {r.headers.get('content-type', '')} {len(r.content)} bytes")
            try:
                datos = r.json()
                print("    JSON:", describir(datos))
                print("    Muestra:", json.dumps(datos, ensure_ascii=False)[:1500])
            except ValueError:
                print("    Texto:", re.sub(r"\s+", " ", r.text[:400]))


def main():
    print("== Cómo pide los datos la gráfica en tiempo real ==")
    llamadas_en_scripts()
    print("\n== Pruebas con POST ==")
    pruebas_post()
    print("\n== robots.txt ==")
    resumir(f"{BASE}/robots.txt", 2000)

    encontradas = set()
    for pagina in PAGINAS:
        print(f"\n== Página: {pagina}")
        r = resumir(pagina, 300)
        if r is None or not r.ok:
            continue
        titulo = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
        print("  Título:", titulo.group(1).strip() if titulo else "—")
        rutas = rutas_en(r.text)
        print(f"  Rutas de datos en el HTML ({len(rutas)}):", rutas[:40])
        encontradas.update(urljoin(r.url, ruta) for ruta in rutas)
        scripts = re.findall(r"""<script[^>]+src=["']([^"']+)["']""", r.text, re.I)
        print(f"  Scripts ({len(scripts)}):", scripts[:20])
        for src in scripts[:15]:
            url = urljoin(r.url, src)
            if urlparse(url).netloc != urlparse(BASE).netloc:
                continue
            try:
                js = SESION.get(url, timeout=30).text
            except requests.RequestException as exc:
                print(f"  ERROR script {url}: {exc}")
                continue
            rutas = rutas_en(js)
            if rutas:
                print(f"  Rutas en {src} ({len(rutas)}):", rutas[:60])
                encontradas.update(urljoin(url, ruta) for ruta in rutas)
            for trozo in sorted(set(re.findall(r".{0,80}(?:2630|station|WaterTemp).{0,80}", js)))[:10]:
                print("    ·", trozo.strip()[:170])

    print("\n== Rutas encontradas que mencionan la estación o datos en tiempo real ==")
    interesantes = [u for u in sorted(encontradas) if re.search(r"(?i)rt|real|last|data|station|estacion", u)]
    for url in interesantes[:40]:
        print(" -", url)

    print("\n== Prueba de direcciones candidatas ==")
    for url in CANDIDATAS + [u for u in interesantes if "{" not in u and "?" not in u][:15]:
        resumir(url, 500)


if __name__ == "__main__":
    main()
