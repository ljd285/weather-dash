"""
Notifica por email los avisos meteorológicos de AEMET (Meteoalerta) de las
zonas de config.py, usando issues de GitHub: GitHub envía un correo al
mencionar a un usuario, así que no hace falta ningún servicio de correo.

- Cada episodio de aviso (una zona y un fenómeno, p. ej. "lluvias en el
  Litoral norte de Valencia") es un issue con la etiqueta ETIQUETA. Los
  tramos del mismo episodio que se solapan (AEMET suele emitir amarillo de
  10 a 14 y naranja de 14 a 20, por ejemplo) van en el mismo issue, con el
  nivel más alto en el título.
- Un episodio nuevo abre un issue; si cambia (sube o baja de nivel, cambian
  las horas) se comenta; si AEMET lo retira antes de tiempo se comenta y se
  cierra; si termina a su hora se cierra sin comentario.
- El estado de cada episodio se guarda oculto en el propio issue (un
  comentario HTML en la descripción), sin archivos aparte.

Uso (lo ejecuta el workflow avisos.yml cada 15 minutos):
    AEMET_API_KEY=... GITHUB_TOKEN=... GITHUB_REPOSITORY=usuario/repo python avisos_notificar.py
Con AVISOS_SIMULACRO=1 solo muestra lo que haría, sin tocar GitHub.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

import fetch_weather_dash as f
from config import AVISOS_NIVEL_MINIMO, AVISOS_NOTIFICAR_A, STATIONS

ETIQUETA = "aviso-aemet"
MARCA_ESTADO = "aviso-aemet-estado"
# Tramos del mismo fenómeno separados por menos de esto se unen en un episodio.
HUECO_MAXIMO = timedelta(hours=3)
# Diferencia de horario a partir de la que se notifica un cambio.
CAMBIO_MINIMO = timedelta(minutes=30)
# Un episodio que desaparece de AEMET antes de su fin se da por retirado
# cuando sigue sin aparecer pasado este tiempo (dos comprobaciones): así un
# fallo puntual de AEMET no parece una cancelación.
ESPERA_RETIRADA = timedelta(minutes=25)
COLORES = {"amarillo": "🟡", "naranja": "🟠", "rojo": "🔴"}


# --- Episodios --------------------------------------------------------------

def episodios(avisos, zona, nivel_minimo=AVISOS_NIVEL_MINIMO):
    """Agrupa los avisos vigentes de una zona en episodios: mismo fenómeno
    y tramos que se solapan o están a menos de HUECO_MAXIMO. Cada episodio:
    {zona, fenomeno, nivel, inicio, fin, tramos}."""
    minimo = f.NIVELES_AVISO[nivel_minimo]
    por_fenomeno = {}
    for aviso in avisos:
        if f.NIVELES_AVISO[aviso["nivel"]] < minimo or not aviso["inicio"] or not aviso["fin"]:
            continue
        por_fenomeno.setdefault(f._normalizar(aviso["fenomeno"]), []).append(aviso)

    resultado = []
    for tramos in por_fenomeno.values():
        tramos.sort(key=lambda a: a["inicio"])
        grupo = [tramos[0]]
        for tramo in tramos[1:]:
            if tramo["inicio"] <= max(t["fin"] for t in grupo) + HUECO_MAXIMO:
                grupo.append(tramo)
            else:
                resultado.append(_episodio(grupo, zona))
                grupo = [tramo]
        resultado.append(_episodio(grupo, zona))
    return resultado


def _episodio(tramos, zona):
    nivel = max((t["nivel"] for t in tramos), key=lambda n: f.NIVELES_AVISO[n])
    return {
        "zona": zona,
        "fenomeno": tramos[0]["fenomeno"],
        "nivel": nivel,
        "inicio": min(t["inicio"] for t in tramos),
        "fin": max(t["fin"] for t in tramos),
        "tramos": [
            {"nivel": t["nivel"], "inicio": t["inicio"], "fin": t["fin"], "descripcion": t["descripcion"]}
            for t in tramos
        ],
    }


def _clave(episodio):
    return (f._normalizar(episodio["zona"]), f._normalizar(episodio["fenomeno"]))


def _se_solapan(a, b):
    return a["inicio"] <= b["fin"] + HUECO_MAXIMO and a["fin"] >= b["inicio"] - HUECO_MAXIMO


# --- Decisión (sin red: es lo que prueban los tests) ------------------------

def decidir(actuales, abiertos, ahora, consulta_ok=True):
    """Compara los episodios actuales de AEMET con los issues abiertos y
    devuelve la lista de acciones a realizar:
      ("crear", episodio)
      ("actualizar", numero, episodio, cambios)   # cambios: textos para el comentario
      ("sin_cambios", numero, episodio)           # solo si estaba marcado como ausente
      ("marcar_ausente", numero, estado)
      ("retirar", numero, estado)
      ("cerrar_terminado", numero)
    `abiertos` es una lista de (numero, estado) y `consulta_ok` indica si la
    consulta a AEMET funcionó: si no, no se da nada por retirado."""
    acciones = []
    emparejados = set()
    for episodio in actuales:
        candidatos = [
            (numero, estado) for numero, estado in abiertos
            if numero not in emparejados and _clave(estado) == _clave(episodio) and _se_solapan(estado, episodio)
        ]
        if not candidatos:
            acciones.append(("crear", episodio))
            continue
        numero, estado = candidatos[0]
        emparejados.add(numero)
        cambios = describir_cambios(estado, episodio, ahora)
        if cambios:
            acciones.append(("actualizar", numero, episodio, cambios))
        elif estado.get("ausente_desde"):
            acciones.append(("sin_cambios", numero, episodio))

    for numero, estado in abiertos:
        if numero in emparejados or not consulta_ok:
            continue
        if estado["fin"] <= ahora:
            acciones.append(("cerrar_terminado", numero))
        elif not estado.get("ausente_desde"):
            acciones.append(("marcar_ausente", numero, estado))
        elif ahora - estado["ausente_desde"] >= ESPERA_RETIRADA:
            acciones.append(("retirar", numero, estado))
    return acciones


def describir_cambios(antes, despues, ahora=None):
    """Frases con lo que ha cambiado de un episodio, o lista vacía. Un
    cambio de la hora de inicio cuando el episodio ya había empezado (AEMET
    a veces reemite los avisos en vigor con otro inicio) no se notifica."""
    ahora = ahora or datetime.now(timezone.utc)
    cambios = []
    n_antes, n_despues = f.NIVELES_AVISO[antes["nivel"]], f.NIVELES_AVISO[despues["nivel"]]
    if n_despues > n_antes:
        cambios.append(f"⬆️ Sube a nivel **{despues['nivel']}** {COLORES[despues['nivel']]}")
    elif n_despues < n_antes:
        cambios.append(f"⬇️ Baja a nivel **{despues['nivel']}** {COLORES[despues['nivel']]}")
    ya_empezado = antes["inicio"] <= ahora and despues["inicio"] <= ahora
    if abs(despues["inicio"] - antes["inicio"]) >= CAMBIO_MINIMO and not ya_empezado:
        cambios.append(f"Ahora empieza el {f._momento_aviso(despues['inicio'])} (antes, {f._momento_aviso(antes['inicio'])})")
    if despues["fin"] - antes["fin"] >= CAMBIO_MINIMO:
        cambios.append(f"Se amplía hasta el {f._momento_aviso(despues['fin'])} (antes, {f._momento_aviso(antes['fin'])})")
    elif antes["fin"] - despues["fin"] >= CAMBIO_MINIMO:
        cambios.append(f"Se adelanta el fin al {f._momento_aviso(despues['fin'])} (antes, {f._momento_aviso(antes['fin'])})")
    if not cambios and _firma_tramos(antes, ahora) != _firma_tramos(despues, ahora):
        cambios.append("Cambian los tramos del aviso (ver el detalle abajo)")
    return cambios


def _firma_tramos(episodio, ahora):
    """Niveles y horas de los tramos (instantes, no texto; el inicio de un
    tramo ya empezado no cuenta, por lo mismo que en describir_cambios)."""
    return [
        (t["nivel"], None if t["inicio"] <= ahora else t["inicio"].timestamp(), t["fin"].timestamp())
        for t in episodio.get("tramos", [])
    ]


# --- Texto de los issues ----------------------------------------------------

def titulo(episodio):
    nivel = episodio["nivel"]
    return (
        f"{COLORES[nivel]} Aviso {nivel} por {episodio['fenomeno']} – {episodio['zona']} "
        f"({f._momento_aviso(episodio['inicio'])} a {f._momento_aviso(episodio['fin'])})"
    )


def cuerpo(episodio, mencion=True, ausente_desde=None):
    lineas = []
    if mencion and AVISOS_NOTIFICAR_A:
        lineas.append(f"@{AVISOS_NOTIFICAR_A}\n")
    lineas.append(f"**Aviso {episodio['nivel']} por {episodio['fenomeno']}** en **{episodio['zona']}**, "
                  f"del {f._momento_aviso(episodio['inicio'])} al {f._momento_aviso(episodio['fin'])} (hora peninsular).\n")
    for tramo in episodio["tramos"]:
        lineas.append(f"- {COLORES[tramo['nivel']]} **{tramo['nivel']}**, del {f._momento_aviso(tramo['inicio'])} "
                      f"al {f._momento_aviso(tramo['fin'])}" + (f": {tramo['descripcion']}" if tramo["descripcion"] else ""))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    enlaces = ["[Avisos en aemet.es](https://www.aemet.es/es/eltiempo/prediccion/avisos)"]
    if "/" in repo:
        usuario, nombre = repo.split("/", 1)
        enlaces.append(f"[Dashboard](https://{usuario}.github.io/{nombre}/)")
    lineas.append("\n" + " · ".join(enlaces))
    lineas.append("\n_Aviso automático a partir de AEMET Meteoalerta. Ante un aviso rojo, sigue los canales oficiales "
                  "(AEMET, 112, ES-Alert)._")
    lineas.append(f"\n<!-- {MARCA_ESTADO} {json.dumps(_estado(episodio, ausente_desde), ensure_ascii=False)} -->")
    return "\n".join(lineas)


def _estado(episodio, ausente_desde=None):
    return {
        "zona": episodio["zona"], "fenomeno": episodio["fenomeno"], "nivel": episodio["nivel"],
        "inicio": episodio["inicio"].isoformat(), "fin": episodio["fin"].isoformat(),
        "tramos": [{"nivel": t["nivel"], "inicio": t["inicio"].isoformat(), "fin": t["fin"].isoformat(),
                    "descripcion": t["descripcion"]} for t in episodio.get("tramos", [])],
        "ausente_desde": ausente_desde.isoformat() if ausente_desde else None,
    }


def leer_estado(texto):
    """Estado guardado en la descripción de un issue, o None."""
    encontrado = re.search(rf"<!-- {MARCA_ESTADO} (.*?) -->", texto or "", re.S)
    if not encontrado:
        return None
    try:
        crudo = json.loads(encontrado.group(1))
        estado = dict(crudo)
        for campo in ("inicio", "fin"):
            estado[campo] = datetime.fromisoformat(crudo[campo])
        estado["ausente_desde"] = datetime.fromisoformat(crudo["ausente_desde"]) if crudo.get("ausente_desde") else None
        estado["tramos"] = [
            dict(t, inicio=datetime.fromisoformat(t["inicio"]), fin=datetime.fromisoformat(t["fin"]))
            for t in crudo.get("tramos", [])
        ]
        return estado
    except (ValueError, KeyError, TypeError):
        return None


# --- GitHub -------------------------------------------------------------------

class GitHub:
    def __init__(self, token, repo, simulacro=False):
        self.url = f"https://api.github.com/repos/{repo}"
        self.simulacro = simulacro
        self.cabeceras = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _pedir(self, metodo, ruta, **kwargs):
        if self.simulacro and metodo != "GET":
            print(f"[simulacro] {metodo} {ruta}: {json.dumps(kwargs.get('json', {}), ensure_ascii=False)[:300]}")
            return {}
        resp = requests.request(metodo, f"{self.url}{ruta}", headers=self.cabeceras, timeout=30, **kwargs)
        if metodo == "POST" and ruta == "/labels" and resp.status_code == 422:
            return {}  # la etiqueta ya existe
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def asegurar_etiqueta(self):
        self._pedir("POST", "/labels", json={"name": ETIQUETA, "color": "FF9800", "description": "Aviso meteorológico de AEMET"})

    def abiertos(self):
        issues = self._pedir("GET", "/issues", params={"labels": ETIQUETA, "state": "open", "per_page": 100})
        return [(i["number"], estado) for i in issues if "pull_request" not in i and (estado := leer_estado(i.get("body")))]

    def crear(self, titulo_issue, cuerpo_issue):
        return self._pedir("POST", "/issues", json={"title": titulo_issue, "body": cuerpo_issue, "labels": [ETIQUETA]})

    def comentar(self, numero, texto):
        return self._pedir("POST", f"/issues/{numero}/comments", json={"body": texto})

    def editar(self, numero, **campos):
        return self._pedir("PATCH", f"/issues/{numero}", json=campos)


def aplicar(acciones, github, ahora):
    mencion = f"@{AVISOS_NOTIFICAR_A} " if AVISOS_NOTIFICAR_A else ""
    for accion in acciones:
        tipo = accion[0]
        if tipo == "crear":
            episodio = accion[1]
            github.crear(titulo(episodio), cuerpo(episodio))
            print(f"Aviso nuevo notificado: {titulo(episodio)}")
        elif tipo == "actualizar":
            _, numero, episodio, cambios = accion
            github.comentar(numero, mencion + "**Actualización del aviso:**\n\n" + "\n".join(f"- {c}" for c in cambios))
            github.editar(numero, title=titulo(episodio), body=cuerpo(episodio))
            print(f"Aviso #{numero} actualizado: {'; '.join(cambios)}")
        elif tipo == "sin_cambios":
            _, numero, episodio = accion
            github.editar(numero, body=cuerpo(episodio))  # vuelve a aparecer: se quita la marca de ausencia
        elif tipo == "marcar_ausente":
            _, numero, estado = accion
            github.editar(numero, body=cuerpo(estado, ausente_desde=ahora))
            print(f"Aviso #{numero} no aparece en AEMET; se confirmará en la próxima comprobación.")
        elif tipo == "retirar":
            _, numero, estado = accion
            github.comentar(numero, mencion + "✅ **AEMET ha retirado este aviso** antes de la hora prevista de fin.")
            github.editar(numero, state="closed", state_reason="not_planned")
            print(f"Aviso #{numero} retirado por AEMET: cerrado.")
        elif tipo == "cerrar_terminado":
            github.editar(accion[1], state="closed", state_reason="completed")
            print(f"Aviso #{accion[1]} terminado: cerrado.")


def main():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    simulacro = os.environ.get("AVISOS_SIMULACRO") == "1"
    if not os.environ.get("AEMET_API_KEY") or not repo or not (token or simulacro):
        sys.exit("ERROR: faltan AEMET_API_KEY, GITHUB_REPOSITORY o GITHUB_TOKEN.")

    ahora = datetime.now(timezone.utc)
    zonas = sorted({(e["area_avisos"], e["zona_avisos"]) for e in STATIONS if e.get("area_avisos") and e.get("zona_avisos")})
    actuales, consulta_ok, por_area = [], True, {}
    for area, zona in zonas:
        try:
            if area not in por_area:
                por_area[area] = f.obtener_avisos(area)
            actuales.extend(episodios(f.avisos_para_zona(por_area[area], zona, ahora), zona))
        except Exception as exc:
            consulta_ok = False
            print(f"Aviso: no se pudieron consultar los avisos del área {area}: {exc}")

    github = GitHub(token, repo, simulacro)
    github.asegurar_etiqueta()
    acciones = decidir(actuales, github.abiertos(), ahora, consulta_ok)
    print(f"{len(actuales)} episodio(s) de aviso vigentes; {len(acciones)} acción(es).")
    aplicar(acciones, github, ahora)


if __name__ == "__main__":
    main()
