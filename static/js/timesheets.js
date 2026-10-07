(() => {
  if (window.parent !== window) {
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') window.parent.postMessage({kind: 'punch-detail-close'}, location.origin);
    });
  }
  const dialog = document.querySelector('[data-timesheet-dialog]');
  if (!dialog || !dialog.showModal) return;
  const body = dialog.querySelector('[data-timesheet-body]');
  let opener;
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-punch-detail-link]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button) return;
    event.preventDefault();
    opener = link;
    const frame = document.createElement('iframe');
    frame.title = 'Punch evidence, breaks, holdovers, and corrections';
    const url = new URL(link.href);
    url.searchParams.set('modal', '1');
    frame.src = url.href;
    body.replaceChildren(frame);
    dialog.showModal();
    dialog.querySelector('[data-timesheet-close]').focus();
  });
  dialog.querySelector('[data-timesheet-close]').addEventListener('click', () => dialog.close());
  window.addEventListener('message', event => {
    const frame = body.querySelector('iframe');
    if (event.origin === location.origin && event.source === frame?.contentWindow &&
        event.data?.kind === 'punch-detail-close') dialog.close();
  });
  dialog.addEventListener('close', () => {
    body.replaceChildren();
    opener?.focus();
    // Reload evidence after a modal correction, retaining the current filters.
    location.reload();
  });
})();
