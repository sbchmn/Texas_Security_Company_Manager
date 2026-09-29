if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/service-worker.js'));
document.querySelector('[data-menu]')?.addEventListener('click', () => document.body.classList.toggle('menu-open'));
const search = document.querySelector('[data-table-search]');
search?.addEventListener('input', () => { const q=search.value.toLowerCase(); document.querySelectorAll('[data-search-row]').forEach(row => row.hidden=!row.textContent.toLowerCase().includes(q)); });

const clock = document.querySelector('[data-clock]');
if (clock) {
  const status = clock.querySelector('[data-clock-status]');
  const now = clock.querySelector('[data-clock-now]');
  const csrf = clock.querySelector('[name=csrfmiddlewaretoken]').value;
  const queueKey = 'tscm-punch-queue-v1';
  const renderTime = () => { now.textContent = new Intl.DateTimeFormat([], {weekday:'long', hour:'numeric', minute:'2-digit', second:'2-digit'}).format(new Date()); };
  renderTime(); setInterval(renderTime, 1000);
  const queued = () => JSON.parse(localStorage.getItem(queueKey) || '[]');
  const saveQueue = items => localStorage.setItem(queueKey, JSON.stringify(items));
  const send = async payload => {
    const response = await fetch(clock.dataset.endpoint, {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf}, body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Punch was not accepted.');
    return result;
  };
  const flush = async () => {
    if (!navigator.onLine) return;
    const items=queued(), remaining=[];
    for (const item of items) { try { await send({...item, offline:true}); } catch (error) { remaining.push(item); } }
    saveQueue(remaining);
    if (items.length && !remaining.length) status.textContent='Offline punches synchronized.';
  };
  clock.querySelectorAll('[data-punch]').forEach(button => button.addEventListener('click', () => {
    button.disabled=true; status.textContent='Capturing location…';
    const complete = async position => {
      const payload={client_event_id:crypto.randomUUID(), kind:button.dataset.punch, occurred_at:new Date().toISOString(), shift_id:clock.querySelector('[data-shift]').value || null, latitude:position?.coords?.latitude ?? null, longitude:position?.coords?.longitude ?? null, offline:!navigator.onLine};
      try { const result=await send(payload); status.textContent=result.exception || `${button.textContent} recorded.`; }
      catch(error) { if (!navigator.onLine || error instanceof TypeError) { saveQueue([...queued(),payload]); status.textContent='Saved offline. It will sync when connectivity returns.'; } else status.textContent=error.message; }
      finally { button.disabled=false; }
    };
    if (navigator.geolocation) navigator.geolocation.getCurrentPosition(complete,()=>complete(null),{enableHighAccuracy:true,timeout:10000,maximumAge:0}); else complete(null);
  }));
  window.addEventListener('online',flush); flush();
}
