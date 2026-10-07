(() => {
  const button = document.querySelector('[data-map-load]');
  if (!button) return;
  const container = document.querySelector('[data-punch-map]');
  const status = document.querySelector('[data-map-status]');
  const data = JSON.parse(document.getElementById('punch-map-data').textContent);
  const script = src => new Promise((resolve, reject) => {
    const element = document.createElement('script');
    element.src = src;
    element.onload = resolve;
    element.onerror = () => reject(new Error('Map library could not load. Coordinates remain available above.'));
    document.head.append(element);
  });
  let loading = false;
  button.addEventListener('click', async event => {
    event.preventDefault();
    if (loading) return;
    loading = true;
    status.textContent = 'Loading location map…';
    container.hidden = false;
    try {
      const hasSite = data.site_lat !== null && data.site_lng !== null;
      if (data.provider === 'osm') {
        const css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = button.dataset.mapStyle;
        document.head.append(css);
        await script(button.dataset.mapLibrary);
        const map = L.map(container).setView([data.lat, data.lng], 16);
        const attribution = document.createElement('span');
        attribution.textContent = data.attribution;
        const tiles = L.tileLayer(data.tile_url, {maxZoom: 19, attribution: attribution.innerHTML}).addTo(map);
        tiles.on('tileerror', () => { status.textContent = 'Some map tiles could not load. Coordinates remain available above.'; });
        L.circleMarker([data.lat, data.lng], {radius: 7, color: '#b91c1c'}).addTo(map).bindTooltip('Captured punch position');
        if (hasSite) {
          const fence = L.circle([data.site_lat, data.site_lng], {radius: data.radius, color: '#1d4ed8'}).addTo(map).bindTooltip('Current site geofence');
          map.fitBounds(fence.getBounds().extend([data.lat, data.lng]), {maxZoom: 17, padding: [20, 20]});
        }
        status.textContent = 'Red: captured position. Blue: current site geofence, when configured.';
      } else if (data.provider === 'google') {
        window.gm_authFailure = () => { status.textContent = 'Google Maps authorization failed. Check the configured key and domain restrictions.'; };
        await script(`https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(data.key)}&loading=async`);
        const {Map, Circle} = await google.maps.importLibrary('maps');
        const map = new Map(container, {center: {lat: data.lat, lng: data.lng}, zoom: 16, mapTypeControl: false});
        new Circle({map, center: {lat: data.lat, lng: data.lng}, radius: 5, strokeColor: '#b91c1c', fillColor: '#b91c1c', fillOpacity: 1});
        if (hasSite) {
          const fence = new Circle({map, center: {lat: data.site_lat, lng: data.site_lng}, radius: data.radius, strokeColor: '#1d4ed8'});
          const bounds = fence.getBounds();
          bounds.extend({lat: data.lat, lng: data.lng});
          map.fitBounds(bounds);
        }
        status.textContent = 'Red: captured position. Blue: current site geofence, when configured.';
      } else {
        throw new Error('Maps are disabled.');
      }
      button.hidden = true;
    } catch (error) {
      status.textContent = error.message;
      loading = false;
    }
  });
})();
