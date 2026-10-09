const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/mobile-layout.js'), 'utf8');
const cell = (text, options = {}) => ({
  textContent: text, tagName: 'TD', colSpan: 1, rowSpan: 1, children: [], attributes: {},
  setAttribute(name, value) {this.attributes[name] = value;},
  prepend(child) {this.children.unshift(child);},
  querySelector() {return this.interactive ? {} : null;},
  ...options,
});
const group = rows => ({rows, attributes: {}, setAttribute(name, value) {this.attributes[name] = value;}});
const row = cells => ({...group([]), cells});
function table(headers, rows) {
  return {
    ...group([]), tHead: group([row(headers)]), tBodies: [group(rows)], classes: new Set(),
    classList: {add(name) {this.owner.classes.add(name);}},
  };
}
function prepare(tables) {
  tables.forEach(table => {table.classList.owner = table;});
  vm.runInNewContext(source, {document: {
    querySelectorAll: () => tables,
    createElement: () => cell(''),
  }});
}

test('labels every ordinary value and action without replacing contents', () => {
  const value = cell('Solomon'), action = cell('Edit', {interactive: true});
  const t = table([cell('Officer'), cell('')], [row([value, action])]);
  prepare([t]);
  assert.ok(t.classes.has('mobile-table'));
  assert.equal(value.children[0].textContent, 'Officer');
  assert.equal(value.textContent, 'Solomon');
  assert.equal(action.children[0].textContent, 'Actions');
  assert.equal(action.children[0].attributes['aria-hidden'], 'true');
  assert.equal(t.attributes.role, 'table');
  assert.equal(t.tHead.rows[0].cells[0].attributes.role, 'columnheader');
  assert.equal(value.attributes.role, 'cell');
});

test('full-width empty/summary rows do not acquire misleading labels', () => {
  const empty = cell('No records', {colSpan: 3});
  const t = table([cell('Person'), cell('Status'), cell('Actions')], [row([empty])]);
  prepare([t]);
  assert.ok(t.classes.has('mobile-table'));
  assert.equal(empty.children.length, 0);
  assert.equal(empty.textContent, 'No records');
});

test('partial column spans combine the relevant headers', () => {
  const combined = cell('Jane / Main site', {colSpan: 2});
  const t = table([cell('Officer'), cell('Site'), cell('Actions')], [row([combined, cell('Edit')])]);
  prepare([t]);
  assert.equal(combined.children[0].textContent, 'Officer / Site');
});

test('row headers retain their semantic role and filtered rows stay hidden', () => {
  const identity = cell('Jane', {tagName: 'TH'});
  const hiddenRow = row([identity, cell('Active')]);
  hiddenRow.hidden = true;
  const t = table([cell('Officer'), cell('Status')], [hiddenRow]);
  prepare([t]);
  assert.equal(identity.attributes.role, 'rowheader');
  assert.equal(hiddenRow.hidden, true);
});

test('complex or malformed tables retain their original layout', () => {
  const rowspan = table([cell('Person'), cell('Status')], [row([cell('Jane', {rowSpan: 2}), cell('Active')])]);
  const mismatch = table([cell('Person'), cell('Status')], [row([cell('Jane')])]);
  const headerSpan = table([cell('Person', {colSpan: 2})], [row([cell('Jane'), cell('Active')])]);
  const multipleHeaders = table([cell('Person')], [row([cell('Jane')])]);
  multipleHeaders.tHead.rows.push(row([cell('Name')]));
  const noHeader = table([], []);
  noHeader.tHead = null;
  prepare([rowspan, mismatch, headerSpan, multipleHeaders, noHeader]);
  for (const t of [rowspan, mismatch, headerSpan, multipleHeaders, noHeader]) {
    assert.equal(t.classes.size, 0);
    assert.equal(t.attributes.role, undefined);
  }
});
