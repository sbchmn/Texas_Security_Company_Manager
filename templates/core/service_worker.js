const SHELL="tscm-shell-v3-{{ revision }}",DOCS="tscm-docs-v3-{{ revision }}";
const SHELL_ASSETS={{ assets|safe }};
const PAGES=["/clock/"];
const clockKey="/clock/";
const clearDocuments=async()=>{
  const names=await caches.keys();
  await Promise.all(names.filter(name=>name.startsWith("tscm-docs-")).map(name=>caches.delete(name)));
};
self.addEventListener("install",event=>event.waitUntil(
  caches.open(SHELL).then(cache=>cache.addAll(SHELL_ASSETS))
));
self.addEventListener("activate",event=>event.waitUntil((async()=>{
  const names=await caches.keys();
  await Promise.all(names.filter(name=>
    (name.startsWith("tscm-shell-")||name.startsWith("tscm-docs-"))&&name!==SHELL&&name!==DOCS
  ).map(name=>caches.delete(name)));
  await self.clients.claim();
})()));
const announceStale=()=>self.clients.matchAll({type:"window"}).then(list=>
  list.forEach(client=>client.postMessage({type:"clock-served-from-cache"})));
const staleClock=async cached=>{
  const html=(await cached.text()).replace("data-clock data-endpoint", 'data-clock data-clock-stale="true" data-endpoint')
    .replace("data-clock-offline hidden","data-clock-offline");
  const headers=new Headers(cached.headers);
  headers.delete("Content-Length");headers.delete("Content-Encoding");
  headers.set("Cache-Control","no-store");
  await announceStale();
  return new Response(html,{status:200,headers});
};
const offlineNotice=()=>new Response(
  '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TSCM - offline</title><link rel="stylesheet" href="{{ stylesheet|escapejs }}"></head><body><main class="page narrow"><h1>No saved clock page</h1><p>You are offline. Scheduling, records, sign-in and payroll require a connection. Open Time clock once while online on your personal device to prepare offline capture. Existing queued punches are not lost; they remain encrypted on this device. Reconnect and open Time clock within 12 hours to synchronize.</p><p><a href="/clock/">Open Time clock</a> or reconnect and retry.</p></main></body></html>',
  {status:503,headers:{"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store"}}
);
self.addEventListener("fetch",event=>{
  const request=event.request,url=new URL(request.url);
  if(url.origin!==self.location.origin)return;
  if(request.method==="POST"&&["/accounts/logout/","/organizations/select/"].includes(url.pathname)){
    event.respondWith(clearDocuments().then(()=>fetch(request)));
    return;
  }
  if(request.method!=="GET")return;
  if(PAGES.includes(url.pathname)){
    event.respondWith((async()=>{
      try{
        const response=await fetch(request);
        if(response.ok&&response.headers.get("X-TSCM-Offline-Clock")==="1"){
          const cache=await caches.open(DOCS);
          await cache.put(clockKey,response.clone());
          try{
            const theme=await fetch("/theme.css",{redirect:"error"});
            if(theme.ok)await cache.put("/theme.css",theme);
          }catch(error){console.warn("Offline theme unavailable",error);}
        }else{
          await clearDocuments();
        }
        return response;
      }catch(error){
        const cached=await (await caches.open(DOCS)).match(clockKey);
        if(cached)return staleClock(cached);
        return offlineNotice();
      }
    })());
    return;
  }
  if(request.mode==="navigate"){
    event.respondWith(fetch(request).catch(async()=>{
      if(url.pathname==="/"||url.pathname==="/workspace/today/"){
        const cached=await (await caches.open(DOCS)).match(clockKey);
        if(cached)return staleClock(cached);
      }
      return offlineNotice();
    }));
    return;
  }
  if(SHELL_ASSETS.includes(url.pathname)||url.pathname==="/theme.css"){
    event.respondWith(fetch(request).catch(async()=>{
      const cached=await caches.match(request);
      return cached||Response.error();
    }));
  }
});
