const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const {chromium, webkit} = require('playwright');

const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

for (const engine of [chromium, webkit]) {
test(`${engine.name()}: phone/tablet rows keep long values and controls visible; desktop stays tabular`, async () => {
  const browser = await engine.launch({headless: true});
  try {
    const page = await browser.newPage();
    const token = 'LongUnbrokenEmployeeAndSiteName'.repeat(8);
    await page.setContent(`<main><div class="page">
      <section class="panel">
        <div class="schedule-toolbar"><nav class="segmented">
          <a href="#previous">Previous week</a><a href="#current">This week</a><a href="#next">Next week</a>
        </nav></div>
        <form class="search-row"><div class="tags"><label>Subject</label>
          <select><option>${token}</option></select><button class="button" type="button">Filter</button>
        </div></form>
        <input data-table-search aria-label="Search officers">
        <div class="stacked-form"><img class="qr-img" alt="QR fixture" width="190" height="190"></div>
        <div class="table-wrap"><table><thead><tr><th>Officer</th><th>Evidence</th><th></th></tr></thead><tbody>
          <tr data-search-row><td>Jane</td><td>${token}</td><td>
            <form class="decide-form"><label>Decision<select><option>${token}</option></select></label>
            <label>Reason<input value="${token}"></label><button class="button" type="button">Approve with documented reason</button></form>
          </td></tr>
          <tr data-search-row><td>Solomon</td><td>Complete</td><td><a class="button" href="#edit">Edit</a></td></tr>
          <tr><td colspan="3">No more records</td></tr>
        </tbody></table></div>
      </section></div></main>`);
    await page.addStyleTag({content: read('static/css/app.css')});
    await page.addScriptTag({content: read('static/js/mobile-layout.js')});
    await page.addScriptTag({content: read('static/js/app.js')});
    for (const width of [320, 390, 760, 820, 1050]) {
      await page.setViewportSize({width, height: 844});
      const measurements = await page.evaluate(() => ({
        width: innerWidth,
        document: document.documentElement.scrollWidth,
        scrollAreas: [...document.querySelectorAll('main *')].filter(element =>
          element.clientWidth && element.scrollWidth > element.clientWidth + 1 &&
          ['auto', 'scroll'].includes(getComputedStyle(element).overflowX)).map(element => element.className),
        outside: [...document.querySelectorAll('a,button,input,select')].filter(element => {
          const bounds = element.getBoundingClientRect();
          return bounds.width && (bounds.right > innerWidth || bounds.left < 0);
        }).map(element => element.outerHTML),
        overflowing: [...document.querySelectorAll('main *')].filter(element =>
          element.getBoundingClientRect().right > innerWidth + 1 &&
          !element.closest('thead')).map(element => ({
            tag: element.tagName, class: element.className,
            width: element.getBoundingClientRect().width,
            scroll: element.scrollWidth, display: getComputedStyle(element).display,
          })).slice(0, 8),
        wideContents: [...document.querySelectorAll('main *')].filter(element =>
          element.clientWidth && element.scrollWidth > element.clientWidth + 2).map(element => ({
            tag: element.tagName, class: element.className, width: element.clientWidth,
            scroll: element.scrollWidth, wrap: getComputedStyle(element).overflowWrap,
            whiteSpace: getComputedStyle(element).whiteSpace,
          })).slice(0, 12),
      }));
      assert.ok(measurements.document <= width, JSON.stringify(measurements));
      assert.deepEqual(measurements.scrollAreas, [], JSON.stringify(measurements));
      assert.deepEqual(measurements.outside, [], JSON.stringify(measurements));
      assert.equal(await page.locator('.mobile-cell-label').first().isVisible(), true);
    }
    assert.equal(await page.locator('table').getAttribute('role'), 'table');
    assert.equal(await page.locator('.mobile-cell-label').first().getAttribute('aria-hidden'), 'true');
    assert.equal(await page.locator('tbody tr').last().locator('.mobile-cell-label').count(), 0);

    await page.locator('[data-table-search]').fill('Officer');
    assert.equal(await page.locator('[data-search-row]:visible').count(), 0);
    await page.locator('[data-table-search]').fill('Solomon');
    assert.equal(await page.locator('[data-search-row]:visible').count(), 1);
    await page.locator('[data-table-search]').fill('');
    assert.equal(await page.locator('[data-search-row]:visible').count(), 2);

    await page.setViewportSize({width: 1366, height: 900});
    assert.equal(await page.locator('table').evaluate(element => getComputedStyle(element).display), 'table');
    assert.equal(await page.locator('tbody tr').first().evaluate(element => getComputedStyle(element).display), 'table-row');
    assert.equal(await page.locator('.mobile-cell-label').first().isVisible(), false);
    assert.equal(await page.locator('thead').evaluate(element => getComputedStyle(element).position), 'static');
  } finally {
    await browser.close();
  }
});
}
