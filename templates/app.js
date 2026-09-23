(function() {
    var raiz = document.documentElement;
    var boton = document.getElementById('toggle-tema');

    var rampaRacha = ${rampa_racha};

    function coloresGrafico(tema) {
        var esOscuro = tema === 'dark';
        return {
            fondo: esOscuro ? '#1E1E1E' : '#FFFFFF',
            texto: esOscuro ? '#ECECEC' : '#212121',
            rejilla: esOscuro ? '#333333' : '#E5E5E5',
        };
    }

    function actualizarGraficos(tema) {
        var c = coloresGrafico(tema);
        document.querySelectorAll('.plotly-graph-div').forEach(function(div) {
            if (!div.layout) return;
            var actualizacion = { paper_bgcolor: c.fondo, plot_bgcolor: c.fondo, 'font.color': c.texto, separators: ',.' };
            Object.keys(div.layout).forEach(function(clave) {
                if (/^(xaxis|yaxis)\d*$$/.test(clave)) {
                    actualizacion[clave + '.gridcolor'] = c.rejilla;
                    actualizacion[clave + '.linecolor'] = c.rejilla;
                    actualizacion[clave + '.zerolinecolor'] = c.rejilla;
                    if (div.layout[clave] && div.layout[clave].rangeselector) {
                        actualizacion[clave + '.rangeselector.bgcolor'] = c.rejilla;
                        actualizacion[clave + '.rangeselector.font.color'] = c.texto;
                    }
                }
            });
            if (div.layout.annotations && div.layout.annotations.length) {
                actualizacion.annotations = div.layout.annotations.map(function(a) {
                    var copia = Object.assign({}, a);
                    copia.font = Object.assign({}, a.font, { color: c.texto });
                    return copia;
                });
            }
            if (div.layout.polar) {
                var rampa = rampaRacha[tema] || rampaRacha.light;
                var indices = [];
                (div.data || []).forEach(function(traza, i) { if (traza.type === 'barpolar') indices.push(i); });
                if (indices.length) {
                    Plotly.restyle(div, {
                        'marker.color': indices.map(function(_, j) { return rampa[j % rampa.length]; }),
                        'marker.line.color': c.fondo,
                    }, indices);
                }
                actualizacion['polar.bgcolor'] = c.fondo;
                ['radialaxis', 'angularaxis'].forEach(function(eje) {
                    actualizacion['polar.' + eje + '.gridcolor'] = c.rejilla;
                    actualizacion['polar.' + eje + '.linecolor'] = c.rejilla;
                });
            }
            Plotly.relayout(div, actualizacion);
        });
    }

    function actualizarBoton(tema) {
        boton.textContent = tema === 'dark' ? '☀️' : '🌙';
        boton.setAttribute('aria-label', tema === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro');
    }

    var temaActual = raiz.getAttribute('data-theme') || 'light';
    actualizarBoton(temaActual);
    actualizarGraficos(temaActual);

    boton.addEventListener('click', function() {
        var nuevo = raiz.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        raiz.setAttribute('data-theme', nuevo);
        try { localStorage.setItem('tema-aemet', nuevo); } catch (e) {}
        actualizarBoton(nuevo);
        actualizarGraficos(nuevo);
        document.dispatchEvent(new CustomEvent('cambio-tema', { detail: nuevo }));
    });
})();

(function() {
    var selector = document.getElementById('selector-estacion');
    if (!selector) return;  // solo hay una estación, no hace falta selector

    var secciones = document.querySelectorAll('.estacion');

    function mostrarEstacion(slug) {
        secciones.forEach(function(sec) {
            var visible = sec.getAttribute('data-estacion') === slug;
            sec.style.display = visible ? '' : 'none';
            if (visible) {
                sec.querySelectorAll('.plotly-graph-div').forEach(function(div) {
                    if (div.layout && window.Plotly) {
                        Plotly.Plots.resize(div);
                    }
                });
            }
        });
    }

    var slugs = Array.prototype.map.call(secciones, function(sec) { return sec.getAttribute('data-estacion'); });
    function valida(slug) { return slug && slugs.indexOf(slug) !== -1; }
    function desdeEnlace() { return decodeURIComponent((location.hash || '').replace(/^#/, '')); }

    // Prioridad: la estación del enlace (#slug), luego la última elegida.
    var guardada = null;
    try { guardada = localStorage.getItem('estacion-aemet'); } catch (e) {}
    var inicial = valida(desdeEnlace()) ? desdeEnlace() : (valida(guardada) ? guardada : slugs[0]);

    function elegir(slug) {
        selector.value = slug;
        try { localStorage.setItem('estacion-aemet', slug); } catch (e) {}
        // La URL refleja la estación, para poder compartir un enlace directo.
        if (history.replaceState) history.replaceState(null, '', '#' + slug);
        mostrarEstacion(slug);
    }

    elegir(inicial);
    selector.addEventListener('change', function() { elegir(selector.value); });
    window.addEventListener('hashchange', function() { if (valida(desdeEnlace())) elegir(desdeEnlace()); });
})();

(function() {
    var contenedor = document.getElementById('mapa-estaciones');
    if (!contenedor || !window.L) return;

    var estaciones = ${datos_mapa};
    if (!estaciones.length) return;

    var mapa = L.map('mapa-estaciones');
    // Teselas de OpenStreetMap (sin clave). En modo oscuro no se cambia de
    // proveedor: el CSS invierte los colores de las teselas (ver .mapa-estaciones).
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 19,
    }).addTo(mapa);

    var grupo = L.featureGroup();
    estaciones.forEach(function(est) {
        var marcador = L.marker([est.lat, est.lon]).bindPopup(est.nombre);
        if (est.temp) {
            marcador.bindTooltip(est.nombre + ': ' + est.temp, { permanent: true, direction: 'top', offset: [-15, -12] });
        }
        marcador.on('click', function() {
            var selector = document.getElementById('selector-estacion');
            if (!selector) return;
            var tieneOpcion = Array.prototype.some.call(selector.options, function(o) { return o.value === est.slug; });
            if (tieneOpcion) {
                selector.value = est.slug;
                selector.dispatchEvent(new Event('change'));
            }
        });
        marcador.addTo(grupo);
    });
    grupo.addTo(mapa);

    if (estaciones.length === 1) {
        mapa.setView([estaciones[0].lat, estaciones[0].lon], 13);
    } else {
        mapa.fitBounds(grupo.getBounds(), { padding: [30, 30] });
    }
})();

// Al desplegar un bloque plegable, los gráficos de dentro recalculan su tamaño.
document.querySelectorAll('details.bloque-plegable').forEach(function(bloque) {
    bloque.addEventListener('toggle', function() {
        if (!bloque.open || !window.Plotly) return;
        bloque.querySelectorAll('.plotly-graph-div').forEach(function(div) { Plotly.Plots.resize(div); });
    });
});

if ("serviceWorker" in navigator) {
    window.addEventListener("load", function() {
        navigator.serviceWorker.register("sw.js").catch(function(error) {
            console.log("No se pudo registrar el service worker:", error);
        });
    });
}
