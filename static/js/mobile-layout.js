(() => {
  document.querySelectorAll('.table-wrap > table').forEach(table => {
    if (table.tHead?.rows.length !== 1) return;
    const headers = [...table.tHead.rows[0].cells];
    const rows = [...table.tBodies].flatMap(body => [...body.rows]);
    if (headers.some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1) ||
        rows.some(row => [...row.cells].some(cell => cell.rowSpan !== 1) ||
          [...row.cells].reduce((total, cell) => total + cell.colSpan, 0) !== headers.length)) return;

    // Explicit roles preserve table navigation when phone CSS changes the display layout.
    table.setAttribute('role', 'table');
    table.tHead.setAttribute('role', 'rowgroup');
    table.tHead.rows[0].setAttribute('role', 'row');
    headers.forEach(header => {
      header.setAttribute('role', 'columnheader');
      header.scope = 'col';
    });
    [...table.tBodies].forEach(body => body.setAttribute('role', 'rowgroup'));
    rows.forEach(row => {
      row.setAttribute('role', 'row');
      let column = 0;
      [...row.cells].forEach(cell => {
        cell.setAttribute('role', cell.tagName === 'TH' ? 'rowheader' : 'cell');
        const label = headers.slice(column, column + cell.colSpan)
          .map(header => header.textContent.trim()).filter(Boolean).join(' / ');
        column += cell.colSpan;
        if (cell.colSpan === headers.length && headers.length > 1) return;
        const heading = document.createElement('span');
        heading.className = 'mobile-cell-label';
        heading.setAttribute('aria-hidden', 'true');
        heading.textContent = label || (cell.querySelector('a,button,input,select') ? 'Actions' : 'Details');
        cell.prepend(heading);
      });
    });
    table.classList.add('mobile-table');
  });
})();
