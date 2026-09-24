// Service worker mínimo para Meteo VLC.
// Estrategia: red primero (para tener siempre los datos más recientes al
// abrir la app con conexión); si la petición falla (sin conexión), se
// devuelve la última copia guardada en caché como reserva.
//
// Solo se guardan en caché la propia página y las librerías que necesita
// (Plotly, Leaflet, tipografía). Las teselas del mapa no se guardan: son miles de
// imágenes y harían crecer la caché sin límite.

const CACHE_NAME = "meteo-vlc-v5";
const URLS_NUCLEO = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-192.png",
  "./icon-maskable-512.png",
];
const ORIGENES_LIBRERIAS = ["https://cdn.plot.ly", "https://unpkg.com", "https://fonts.googleapis.com", "https://fonts.gstatic.com"];

function seGuarda(url) {
  return url.origin === self.location.origin || ORIGENES_LIBRERIAS.includes(url.origin);
}

self.addEventListener("install", (evento) => {
  evento.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS_NUCLEO))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches.keys().then((nombres) =>
      Promise.all(
        nombres.filter((nombre) => nombre !== CACHE_NAME).map((nombre) => caches.delete(nombre))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (evento) => {
  if (evento.request.method !== "GET") return;
  if (!seGuarda(new URL(evento.request.url))) return;  // p. ej. teselas del mapa: directo a la red

  evento.respondWith(
    fetch(evento.request)
      .then((respuesta) => {
        if (respuesta.ok) {
          const copia = respuesta.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(evento.request, copia));
        }
        return respuesta;
      })
      .catch(() => caches.match(evento.request))
  );
});
