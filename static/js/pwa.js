(() => {
  const status = document.querySelector('[data-pwa-status]');
  const install = document.querySelector('[data-pwa-install]');
  const update = document.querySelector('[data-pwa-update]');
  const standalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  let prompt = null;
  const say = text => { if (status) status.textContent = text; };
  say(standalone() ? 'TSCM is running as an installed app.' :
    'Use your browser menu to install, or follow the Android / iPhone instructions below.');
  window.addEventListener('beforeinstallprompt', event => {
    if (!install) return;
    event.preventDefault();
    prompt = event;
    if (install && !standalone()) install.hidden = false;
    say('This browser can install TSCM. Choose Install TSCM when you are ready.');
  });
  install?.addEventListener('click', async () => {
    if (!prompt) return;
    install.disabled = true;
    try {
      await prompt.prompt();
      const choice = await prompt.userChoice;
      say(choice.outcome === 'accepted' ? 'Installation accepted. Follow your browser instructions.' :
        'Installation cancelled. You can install later from the browser menu.');
    } catch (error) {
      say(`Installation could not start: ${error.message}. Use the browser menu instead.`);
    } finally {
      prompt = null;
      install.hidden = true;
      install.disabled = false;
    }
  });
  window.addEventListener('appinstalled', () => {
    if (install) install.hidden = true;
    prompt = null;
    say('TSCM installed. Open it from your home screen.');
  });
  if (!('serviceWorker' in navigator) || !window.isSecureContext) {
    say('Installation and offline preparation require HTTPS and a browser with service-worker support.');
    return;
  }
  const reportUpdate = registration => {
    if (registration.waiting && update) update.hidden = false;
  };
  window.addEventListener('load', async () => {
    let delayed = false;
    const deadline = window.setTimeout(() => {
      delayed = true;
      say('Offline preparation has not finished. Stay connected, retry in a supported browser, or contact your administrator.');
      console.warn('TSCM service-worker registration is still pending.');
    }, 15000);
    try {
      const registration = await navigator.serviceWorker.register('/service-worker.js', {updateViaCache: 'none'});
      if (delayed) say('Offline registration completed. Stay connected while preparing the clock.');
      reportUpdate(registration);
      const watch = worker => {
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) reportUpdate(registration);
          if (worker.state === 'redundant') say('Offline preparation failed. Stay online and retry or contact your administrator.');
        });
      };
      watch(registration.installing);
      registration.addEventListener('updatefound', () => watch(registration.installing));
    } catch (error) {
      console.error('TSCM service-worker registration failed', error);
      say(`Offline preparation failed: ${error.message}. Stay online and contact your administrator.`);
    } finally {
      window.clearTimeout(deadline);
    }
  });
})();
