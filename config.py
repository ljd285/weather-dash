"""
Configuración de estaciones/municipios que se muestran en el dashboard.

Para añadir una nueva localidad en el futuro, agrega otra entrada a STATIONS.
No hace falta conocer el código IDEMA de la estación climatológica: si lo
dejas en None, el script lo busca automáticamente por el texto de
'busqueda_nombre' usando el inventario de estaciones de AEMET (aunque es más
fiable buscarlo tú mismo en la web de AEMET y fijarlo aquí).

El campo 'municipio' es el código INE de 5 dígitos que usa AEMET para la
predicción por municipios — es un código de MUNICIPIO, no de estación
climatológica (p. ej. el Aeropuerto de Valencia está en el municipio de
Manises, no en el de Valencia capital). Se ve en la URL de aemet.es/es/
eltiempo/prediccion/municipios/... -id<codigo>, o buscando el municipio en
https://www.aemet.es/es/eltiempo/prediccion/municipios.

El campo 'idema_tiempo_real' es el código de la estación AUTOMÁTICA que usa
el endpoint de observación en tiempo real — puede ser distinto del 'idema'
climatológico para la misma localidad (p. ej. Valencia es "8416" en el
histórico climatológico pero "8416Y" en observación en tiempo real). Si se
deja en None, se usa el mismo valor que 'idema' como mejor intento.
"""

STATIONS = [
    {
        "nombre": "Valencia",
        "idema": "8416",             # València, Viveros — índice climatológico oficial
                                      # (ver aemet.es/es/serviciosclimaticos/datosclimatologicos/valoresclimatologicos?l=8416).
                                      # OJO: "8416X" (Valencia, UPV) es una estación distinta
                                      # sin histórico climatológico diario completo.
        "idema_tiempo_real": "8416X",  # estación automática (Valencia, UPV) para la observación en tiempo real
        "busqueda_nombre": "VALENCIA",  # ya no se usa mientras 'idema' esté fijado arriba
        "municipio": "46250",       # código INE de Valencia capital
    },
    {
        "nombre": "Valencia Aeropuerto",
        "idema": "8414A",           # Valencia, Aeropuerto — índice climatológico oficial
        "idema_tiempo_real": "8414A",  # de momento se asume el mismo código; confirmar tras la primera ejecución
        "busqueda_nombre": "VALENCIA AEROPUERTO",
        "municipio": "46159",       # código INE de Manises (término municipal del aeropuerto)
    },
    # Ejemplo de cómo añadir otra estación en el futuro:
    # {
    #     "nombre": "Madrid",
    #     "idema": None,
    #     "idema_tiempo_real": None,
    #     "busqueda_nombre": "MADRID, RETIRO",
    #     "municipio": "28079",
    # },
]

# Días de histórico climatológico a mostrar en el dashboard
DIAS_HISTORICO = 90
