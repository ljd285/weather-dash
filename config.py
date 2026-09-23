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

Los campos 'lat'/'lon' (grados decimales) son las coordenadas de la estación
para el mapa. Se pueden ver en la propia página de AEMET de esa estación
(aemet.es/.../ultimosdatos?l=<idema>), en el apartado "Latitud/Longitud".

Los campos 'area_avisos' y 'zona_avisos' sirven para mostrar los avisos
meteorológicos (Meteoalerta) de AEMET. 'area_avisos' es el código de la
comunidad autónoma (61 Andalucía, 62 Aragón, 63 Asturias, 64 Baleares,
65 Canarias, 66 Cantabria, 67 Castilla y León, 68 Castilla-La Mancha,
69 Cataluña, 70 Extremadura, 71 Galicia, 72 Madrid, 73 Murcia, 74 Navarra,
75 País Vasco, 76 La Rioja, 77 Comunitat Valenciana, 78 Ceuta, 79 Melilla).
'zona_avisos' es el nombre exacto de la zona de aviso donde está la
estación, tal como aparece en el mapa de https://www.aemet.es/es/eltiempo/
prediccion/avisos (también vale su código numérico de 6 cifras). Si se deja
en None, no se muestran avisos para esa estación.

El campo opcional 'mar' ({"lat": ..., "lon": ...}) es un punto en el mar,
frente a la costa, donde se consulta la temperatura del agua (modelo de
Copernicus vía Open-Meteo). Conviene que esté a unos kilómetros de la orilla
para que caiga en una celda de mar del modelo. Sin él, no se muestra.
"""

STATIONS = [
    {
        "nombre": "Valencia",
        "idema": "8416",             # València, Viveros — índice climatológico oficial
                                      # (ver aemet.es/es/serviciosclimaticos/datosclimatologicos/valoresclimatologicos?l=8416).
                                      # OJO: "8416X" (Valencia, UPV) es una estación distinta
                                      # sin histórico climatológico diario completo.
        "idema_tiempo_real": "8416",   # mismo código que el climatológico; confirmado en la página de datos
                                        # horarios de AEMET (aemet.es/.../ultimosdatos?l=8416&w=1)
        "busqueda_nombre": "VALENCIA, UPV",  # ya no se usa mientras 'idema' esté fijado arriba
        "municipio": "46250",       # código INE de Valencia capital
        "lat": 39.4805555556,
        "lon": -0.3663888889,
        "area_avisos": "77",        # Comunitat Valenciana
        "zona_avisos": "Litoral norte de Valencia",
        "mar": {"lat": 39.46, "lon": -0.28},  # frente a la playa de la Malva-rosa
    },
    {
        "nombre": "Valencia Aeropuerto",
        "idema": "8414A",           # Valencia, Aeropuerto — índice climatológico oficial
        "idema_tiempo_real": "8414A",  # de momento se asume el mismo código; confirmar tras la primera ejecución
        "busqueda_nombre": "VALENCIA AEROPUERTO",
        "municipio": "46159",       # código INE de Manises (término municipal del aeropuerto)
        "lat": 39.485,
        "lon": -0.474722,
        "area_avisos": "77",
        "zona_avisos": "Litoral norte de Valencia",
        "mar": {"lat": 39.46, "lon": -0.28},  # frente a la playa de la Malva-rosa
    },
    # Ejemplo de cómo añadir otra estación en el futuro:
    # {
    #     "nombre": "Madrid",
    #     "idema": None,
    #     "idema_tiempo_real": None,
    #     "busqueda_nombre": "MADRID, RETIRO",
    #     "municipio": "28079",
    #     "lat": 40.4117,
    #     "lon": -3.6805,
    #     "area_avisos": "72",
    #     "zona_avisos": "Metropolitana y Henares",
    # },
]

# Días de histórico climatológico a mostrar en el dashboard
DIAS_HISTORICO = 90
