if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/service-worker.js'));
document.querySelector('[data-menu]')?.addEventListener('click', event => { const open=document.body.classList.toggle('menu-open'); event.currentTarget.setAttribute('aria-expanded', String(open)); });
const search = document.querySelector('[data-table-search]');
search?.addEventListener('input', () => { const q=search.value.toLowerCase(); document.querySelectorAll('[data-search-row]').forEach(row => row.hidden=!row.textContent.toLowerCase().includes(q)); });

const clock = document.querySelector('[data-clock]');
if (clock) {
  const status=clock.querySelector('[data-clock-status]'), now=clock.querySelector('[data-clock-now]');
  const shiftSelect=clock.querySelector('[data-shift]'), pointSelect=clock.querySelector('[data-checkpoint]');
  const csrf=clock.querySelector('[name=csrfmiddlewaretoken]').value;
  // A patrol point belongs to one site; offering only the points the selected post owns
  // keeps an honest mistake (scan at the wrong property) from being recorded as evidence.
  const filterPoints=()=>{ if(!pointSelect) return; const site=shiftSelect?.selectedOptions[0]?.dataset?.site; for(const option of pointSelect.options){ if(!option.value) continue; option.hidden=Boolean(site)&&option.dataset.site!==site; } };
  shiftSelect?.addEventListener('change',filterPoints); filterPoints();
  const renderTime=()=>{now.textContent=new Intl.DateTimeFormat([],{weekday:'long',hour:'numeric',minute:'2-digit',second:'2-digit'}).format(new Date());}; renderTime();setInterval(renderTime,1000);
  const openDb=()=>new Promise((resolve,reject)=>{const r=indexedDB.open('tscm-secure-clock',1);r.onupgradeneeded=()=>{r.result.createObjectStore('state');r.result.createObjectStore('queue',{keyPath:'id'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
  const tx=(db,store,mode,fn)=>new Promise((resolve,reject)=>{const t=db.transaction(store,mode), req=fn(t.objectStore(store));req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});
  const stateGet=(db,key)=>tx(db,'state','readonly',s=>s.get(key)); const statePut=(db,key,value)=>tx(db,'state','readwrite',s=>s.put(value,key));
  const init=async()=>{const db=await openDb();let key=await stateGet(db,'key');if(!key){key=await crypto.subtle.generateKey({name:'AES-GCM',length:256},false,['encrypt','decrypt']);await statePut(db,'key',key);}let device=await stateGet(db,'device');if(!device&&navigator.onLine){const r=await fetch(clock.dataset.enrollEndpoint,{method:'POST',headers:{'X-CSRFToken':csrf}});if(!r.ok)throw new Error('Unable to enroll this browser for offline clocking.');device=await r.json();await statePut(db,'device',device);}return {db,key,device};};
  const encrypt=async(key,value)=>{const iv=crypto.getRandomValues(new Uint8Array(12)),plain=new TextEncoder().encode(JSON.stringify(value)),cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,plain);return {iv:Array.from(iv),cipher:Array.from(new Uint8Array(cipher))};};
  const decrypt=async(key,item)=>JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:new Uint8Array(item.iv)},key,new Uint8Array(item.cipher))));
  const queue=async(ctx,payload)=>{ctx.device.sequence+=1;payload.device_sequence=ctx.device.sequence;payload.device_token=ctx.device.token;await statePut(ctx.db,'device',ctx.device);const sealed=await encrypt(ctx.key,payload);await tx(ctx.db,'queue','readwrite',s=>s.put({id:payload.client_event_id,...sealed}));};
  const send=async(payload,endpoint=clock.dataset.endpoint)=>{const r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(payload)}),result=await r.json();if(!r.ok)throw new Error(result.error||'Punch was not accepted.');return result;};
  const flush=async ctx=>{if(!navigator.onLine||!ctx.device)return;const items=await tx(ctx.db,'queue','readonly',s=>s.getAll());if(!items.length)return;const sealed=[];for(const item of items){try{sealed.push([item,await decrypt(ctx.key,item)]);}catch(error){status.textContent=`A stored punch could not be decrypted and is still queued: ${error.message}`;}}sealed.sort((a,b)=>((a[1].device_sequence||0)-(b[1].device_sequence||0)));let sent=0,paused=null;for(const [item,payload] of sealed){try{await send(payload,clock.dataset.syncEndpoint);await tx(ctx.db,'queue','readwrite',s=>s.delete(item.id));sent+=1;}catch(error){paused=error.message;break;}}if(paused)status.textContent=`Synchronization paused: ${paused}`;else if(sent)status.textContent=`Offline punches synchronized: ${sent}.`;};
  let context;init().then(ctx=>{context=ctx;flush(ctx);}).catch(e=>status.textContent=e.message);
  // CLK-1. Whether a photo is asked for is decided by the post, not the company, so the panel is
  // driven by the resolved per-site map the view hands over — the same site → contract → company chain
  // the server applies. A officer standing a post whose contract waived the photo never sees a camera.
  const selfieSites=(()=>{try{return JSON.parse(clock.dataset.selfieSites||'{}');}catch(e){return {};}})();
  const selfiePanel=clock.querySelector('[data-selfie-panel]'), selfieVideo=clock.querySelector('[data-selfie-video]'),
        selfieThumb=clock.querySelector('[data-selfie-thumb]'), selfieStatus=clock.querySelector('[data-selfie-status]'),
        selfieTake=clock.querySelector('[data-selfie-take]'), selfieCapture=clock.querySelector('[data-selfie-capture]'),
        selfieRetake=clock.querySelector('[data-selfie-retake]');
  const selfieState={id:null,stream:null};
  const showSelfie=shown=>{if(!selfiePanel)return; selfiePanel.hidden=!shown; if(!shown) resetSelfie();};
  const selfieNeeded=()=>Boolean(selfieSites[shiftSelect?.selectedOptions[0]?.dataset?.site]);
  const resetSelfie=()=>{const previous=selfieState.id; selfieState.id=null;
    selfieState.stream?.getTracks().forEach(track=>track.stop()); selfieState.stream=null;
    if(selfieThumb){selfieThumb.hidden=true;} if(selfieVideo){selfieVideo.hidden=true;}
    if(selfieTake){selfieTake.hidden=false;} if(selfieCapture){selfieCapture.hidden=true;}
    if(selfieRetake){selfieRetake.hidden=true;}
    if(selfieStatus){selfieStatus.textContent='No photo yet.';}
    // An unattached frame is not evidence and must not sit in storage to a three-year clock: the
    // server deletes it, so a retake or a walk-away costs nothing. The id is carried over to the next
    // upload as `replaces`, because a face on the officer's own device must be discarded by the
    // device's next photo, and the id travels with it rather than being deleted on a timer nobody set.
    if(previous) selfieState.discard=previous;}
  const captureFrame=()=>{const canvas=document.createElement('canvas');
    canvas.width=selfieVideo.videoWidth||640; canvas.height=selfieVideo.videoHeight||480;
    // Mirror the preview so what the officer saw is what the file shows; the front camera previews
    // mirrored and records unmirrored, and a reviewer comparing a face to a badge photo should not
    // have to mentally flip one of them.
    const context=canvas.getContext('2d'); context.translate(canvas.width,0); context.scale(-1,1);
    context.drawImage(selfieVideo,0,0,canvas.width,canvas.height);
    canvas.toBlob(async blob=>{
      if(!blob){selfieStatus.textContent='The camera would not give up a frame. Take the photo again.';return;}
      selfieStatus.textContent='Sending the photo…';
      const body=new FormData(); body.append('selfie',blob,'clock-selfie.jpg');
      if(selfieState.discard) body.append('replaces',selfieState.discard);
      try{
        const r=await fetch(clock.dataset.selfieEndpoint,{method:'POST',headers:{'X-CSRFToken':csrf},body});
        const answer=await r.json().catch(()=>({error:'The server could not read the photo.'}));
        if(!r.ok) throw new Error(answer.error||'The photo was not accepted.');
        selfieState.id=answer.document_id; selfieState.discard=null;
        selfieThumb.src=URL.createObjectURL(blob); selfieThumb.hidden=false;
        selfieVideo.hidden=true; selfieTake.hidden=true; selfieCapture.hidden=true; selfieRetake.hidden=false;
        selfieStatus.textContent='Photo attached. Clock in or out within the next couple of minutes.';
      }catch(error){selfieState.id=null; selfieStatus.textContent=error.message;}
    },'image/jpeg',0.85);};
  selfieTake?.addEventListener('click',async()=>{
    if(!navigator.mediaDevices?.getUserMedia){selfieStatus.textContent='This browser will not share a camera. The punch still records and goes to review without a photo.';return;}
    selfieStatus.textContent='Starting the camera…';
    try{
      selfieState.stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user',width:{ideal:1280}},audio:false});
      selfieVideo.srcObject=selfieState.stream; selfieVideo.hidden=false; selfieThumb.hidden=true;
      await selfieVideo.play();
      // The capture button only appears once there is something to capture: a still frame taken from
      // a stream that never started is a black rectangle, and a black rectangle filed as a face photo
      // is worse than no photo because it looks like evidence.
      selfieTake.hidden=true; selfieCapture.hidden=false; selfieStatus.textContent='Hold still, then tap Take the photo.';
    }catch(error){selfieState.stream=null; selfieStatus.textContent='The camera was refused. The punch still records and goes to review without a photo.';}
  });
  selfieCapture?.addEventListener('click',()=>{captureFrame();});
  selfieRetake?.addEventListener('click',()=>{resetSelfie();});
  shiftSelect?.addEventListener('change',()=>showSelfie(selfieNeeded()&&navigator.onLine));
  showSelfie(selfieNeeded()&&navigator.onLine);

  clock.querySelectorAll('[data-punch]').forEach(button=>button.addEventListener('click',()=>{if(button.dataset.punch==='checkpoint'&&pointSelect&&!pointSelect.value){status.textContent='Choose the patrol point you reached before recording the scan.';return;}
    // A queued offline punch cannot carry a frame — the server refuses one, because a photo that
    // arrives with a nine-hour-old queue is not evidence about the moment it claims. So the panel
    // says so rather than letting the officer take a photo that will be thrown away.
    if(selfieNeeded()&&!selfieState.id&&navigator.onLine){status.textContent='No photo yet — this punch will be recorded and sent to review without one.';}
    button.disabled=true;status.textContent='Capturing location…';const complete=async position=>{const ageMs=position?.timestamp?Date.now()-new Date(position.timestamp).getTime():null;const fixAge=(ageMs!==null&&Number.isFinite(ageMs)&&ageMs>=0&&ageMs<21600000)?Math.round(ageMs/1000):null;const payload={client_event_id:crypto.randomUUID(),kind:button.dataset.punch,occurred_at:new Date().toISOString(),shift_id:shiftSelect?.value||null,latitude:position?.coords?.latitude??null,longitude:position?.coords?.longitude??null,accuracy:position?.coords?.accuracy??null,fix_age_seconds:fixAge,offline:!navigator.onLine,selfie_document_id:(navigator.onLine&&selfieState.id)||null};if(button.dataset.punch==='checkpoint'&&pointSelect?.value)payload.checkpoint_code=pointSelect.value;try{if(!context)throw new Error('Secure clock is still starting.');if(navigator.onLine){const result=await send(payload);status.textContent=result.exception||`${button.textContent} recorded.`;}else{await queue(context,payload);status.textContent='Encrypted offline punch saved. It will sync within 12 hours.';}resetSelfie();}catch(error){if((!navigator.onLine||error instanceof TypeError)&&context?.device){await queue(context,payload);status.textContent='Encrypted offline punch saved. It will sync within 12 hours.';resetSelfie();}else status.textContent=error.message;}finally{button.disabled=false;}};navigator.geolocation?navigator.geolocation.getCurrentPosition(complete,()=>complete(null),{enableHighAccuracy:true,timeout:10000,maximumAge:0}):complete(null);}));
  window.addEventListener('online',()=>context&&flush(context));
  // CLK-5. The clock page can now be reopened offline out of the service-worker cache, which is the
  // difference between an installable app and one whose icon merely appears. A cached page still
  // shows the posts as of the last online load and carries a session token that may no longer match,
  // so it has to say it is stale and reload itself the moment signal returns rather than let a
  // guard act on state nobody has confirmed.
  let servedStale=!navigator.onLine;
  const banner=clock.querySelector('[data-clock-offline]');
  const admitStale=()=>{servedStale=true;if(banner)banner.hidden=false;};
  if(servedStale)admitStale();
  if(navigator.serviceWorker)navigator.serviceWorker.addEventListener('message',event=>{if(event.data&&event.data.type==='clock-served-from-cache')admitStale();});
  window.addEventListener('online',()=>{if(servedStale)window.location.reload();});
}

// CLK-2. The shared clock station. Deliberately its own block rather than a mode of the clock above:
// this page has no signed-in user, no offline queue (a PIN cannot be verified without the server, and
// a queue of unverified punches at a lobby tablet is exactly the buddy-punching hole the PIN exists to
// close), and it must hand itself back to a blank pad.
const kiosk = document.querySelector('[data-kiosk]');
if (kiosk) {
  const csrf=kiosk.querySelector('[name=csrfmiddlewaretoken]').value;
  const pad=kiosk.querySelector('[data-kiosk-pad]'), panel=kiosk.querySelector('[data-kiosk-person]'),
        readout=kiosk.querySelector('[data-kiosk-pin]'), status=kiosk.querySelector('[data-kiosk-status]'),
        first=kiosk.querySelector('[data-kiosk-first]'), posts=kiosk.querySelector('[data-kiosk-posts]'),
        pointSelect=kiosk.querySelector('[data-kiosk-checkpoint]');
  const STATE_LABEL={on_post:'on post now',upcoming:'starts',ended:'ended'};
  let typed='', session=null, chosen=null, handBack=null, expires=null;
  const draw=()=>{ readout.textContent = typed ? '•'.repeat(typed.length) : 'Enter your clock PIN'; };
  const reset=()=>{
    clearTimeout(handBack); clearTimeout(expires);
    typed=''; session=null; chosen=null;
    posts.innerHTML=''; if(pointSelect) pointSelect.value='';
    draw(); panel.hidden=true; pad.hidden=false;
    status.textContent='Choose what to record.';
  };
  const ask=async(endpoint,body)=>{
    const r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(body)});
    const result=await r.json().catch(()=>({error:'The station could not read the answer.'}));
    if(!r.ok) throw new Error(result.error||'The station refused the request.');
    return result;
  };
  const readLocation=()=>new Promise(resolve=>{
    // A station sits at its post, so a reading is usually the geofence's own answer. Denied or
    // unavailable resolves to no location rather than blocking the tour: the punch still records and
    // lands in review with "Location was not supplied" when the site requires it, which is the honest
    // outcome and the one a supervisor can act on.
    if(!navigator.geolocation) return resolve({});
    navigator.geolocation.getCurrentPosition(
      position=>{const ageMs=position.timestamp?Date.now()-new Date(position.timestamp).getTime():null;
        resolve({latitude:position.coords.latitude,longitude:position.coords.longitude,accuracy:position.coords.accuracy,
          fix_age_seconds:(Number.isFinite(ageMs)&&ageMs>=0&&ageMs<21600000)?Math.round(ageMs/1000):null});},
      ()=>resolve({}),{enableHighAccuracy:true,timeout:8000,maximumAge:0});
  });
  const identify=async()=>{
    if(typed.length<4){ status.textContent='A PIN is at least four digits.'; return; }
    const pin=typed; typed=''; draw();
    status.textContent='Checking…';
    try{
      const answer=await ask(kiosk.dataset.identifyEndpoint,{pin});
      session=answer.identity;
      first.textContent=answer.first_name;
      pad.hidden=true; panel.hidden=false;
      const list=answer.shifts||[];
      chosen=answer.recommended||(list[0]&&list[0].id)||null;
      posts.replaceChildren(...list.map(entry=>{
        const button=document.createElement('button');
        button.type='button'; button.className='kiosk-post'+(entry.id===chosen?' selected':'');
        button.dataset.postId=entry.id;
        const marks=[STATE_LABEL[entry.state]||entry.state,entry.starts_label+'–'+entry.ends_label,
          entry.punched_in?(entry.punched_out?'clocked in and out':'clocked in'):'not clocked in'];
        button.textContent='';
        const name=document.createElement('strong'); name.textContent=entry.site; button.append(name);
        const note=document.createElement('small'); note.textContent=marks.join(' · '); button.append(note);
        return button;
      }));
      if(!list.length){ status.textContent='No post is scheduled for you in the next twelve hours. Clock in without a post and it will be held for review.'; }
      else status.textContent=chosen?'Your post is already chosen — tap Clock in.':'Choose the post you are standing.';
      // Ninety seconds, then the pad, whatever is on the screen. An officer who walks away mid-
      // interaction must not leave their name in front of the next person in the doorway.
      clearTimeout(expires); expires=setTimeout(reset,Math.max(0,(answer.expires_in||90)-2)*1000);
    }catch(error){ status.textContent=error.message; typed=''; draw(); }
  };
  const punch=async(kind)=>{
    if(!session){ reset(); return; }
    if(kind==='checkpoint'&&pointSelect&&!pointSelect.value){ status.textContent='Choose the patrol point you reached.'; return; }
    const buttons=kiosk.querySelectorAll('[data-kiosk-punch]'); buttons.forEach(button=>button.disabled=true);
    status.textContent='Recording…';
    try{
      const coords=await readLocation();
      const answer=await ask(kiosk.dataset.punchEndpoint,{identity:session,kind,client_event_id:crypto.randomUUID(),
        occurred_at:new Date().toISOString(),shift_id:chosen||null,
        checkpoint_code:kind==='checkpoint'&&pointSelect?pointSelect.value:null,...coords});
      status.textContent=answer.exception||`${kind==='checkpoint'?'Patrol point':kind==='in'?'Clocked in':'Clocked out'} — recorded.`;
      clearTimeout(expires); handBack=setTimeout(reset,7000);
    }catch(error){ status.textContent=error.message; if(/PIN session/.test(error.message)) reset(); }
    finally{ buttons.forEach(button=>button.disabled=false); }
  };
  kiosk.addEventListener('click',event=>{
    const key=event.target.closest('[data-digit]');
    if(key){ if(typed.length<8){ typed+=key.dataset.digit; draw(); } return; }
    if(event.target.closest('[data-kiosk-back]')){ typed=typed.slice(0,-1); draw(); return; }
    if(event.target.closest('[data-kiosk-clear]')){ typed=''; draw(); return; }
    if(event.target.closest('[data-kiosk-go]')){ identify(); return; }
    if(event.target.closest('[data-kiosk-finish]')){ reset(); return; }
    const post=event.target.closest('[data-post-id]');
    if(post){ chosen=post.dataset.postId; posts.querySelectorAll('.kiosk-post').forEach(row=>row.classList.toggle('selected',row===post)); status.textContent='Tap Clock in when you are ready.'; return; }
    const action=event.target.closest('[data-kiosk-punch]');
    if(action){ punch(action.dataset.kioskPunch); }
  });
  // A station with a keyboard is a station an officer can use without touching a screen.
  kiosk.addEventListener('keydown',event=>{
    if(event.key>='0'&&event.key<='9'){ if(typed.length<8){typed+=event.key;draw();} event.preventDefault(); }
    else if(event.key==='Backspace'){ typed=typed.slice(0,-1); draw(); event.preventDefault(); }
    else if(event.key==='Enter'){ identify(); event.preventDefault(); }
    else if(event.key==='Escape'){ reset(); }
  });
  draw(); kiosk.querySelector('[data-kiosk-go]')?.focus();
}

// In-page record preview. One handler for every list, because the alternative is each page
// remembering to wire a button; the access rule is enforced on the route regardless, so this only
// chooses whether the bytes open here or in a browser download.
//
// The frame src is always this app's own /preview/ URL and never a storage link: the redirect to any
// signed link happens in the network layer after the permission check, not in markup a page could get
// wrong. Escape and the backdrop close it.
const preview=document.getElementById('record-preview');
if(preview){
  const frame=preview.querySelector('#record-preview-object'),image=preview.querySelector('#record-preview-image'),
        text=preview.querySelector('#record-preview-text'),title=preview.querySelector('#record-preview-title'),
        note=preview.querySelector('#record-preview-note'),download=preview.querySelector('#record-preview-download');
  const show=kind=>{[frame,image,text].forEach(node=>node.hidden=true);({pdf:frame,image:image,text:text})[kind].hidden=false;};
  const close=()=>{preview.hidden=true;};
  preview.addEventListener('click',event=>{if(event.target===preview||event.target.closest('[data-preview-close]'))close();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!preview.hidden)close();});
  document.querySelectorAll('[data-preview]').forEach(button=>button.addEventListener('click',()=>{
    const url=button.dataset.preview,kind=button.dataset.previewKind||'pdf';
    preview.hidden=false;
    title.textContent=button.dataset.previewTitle||'Document';
    note.textContent=button.dataset.previewNote||'';
    // Derived from the route we were handed rather than recomposed from the row's data, so the
    // preview and its own Download link can never end up pointing at different records.
    if(download)download.href=url.replace(/\/preview\/$/,'/download/');
    if(kind==='text'){
      fetch(url,{credentials:'same-origin'}).then(response=>{if(!response.ok)throw new Error(response.status);return response.text();})
        .then(body=>{text.textContent=body;show('text');}).catch(()=>{show('image');frame.src=url;});
      // Fetched rather than framed so it renders as characters in the <pre> and nothing along the way
      // can decide it is a document.
    }else{show(kind);frame.src=url;}
  }));
}
