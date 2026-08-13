// One-shot migrator: rewrite free references to the old global state
// variables into AppState.<name> members. Scoped: skips identifiers that a
// function declares locally (params, var/let/const, nested function names).
const fs = require('fs');
const acorn = require('./node_modules/acorn');

const src = fs.readFileSync('static/app.js', 'utf8');
const ast = acorn.parse(src, { ecmaVersion: 2022, range: true });

const OWNED = new Set([
  'games', 'playlists', 'filterPresets', 'explorerField', 'explorerRules',
  'activeFilterPreset', 'bigBoxGames', 'runningGames', 'raConfigured',
  'selectedId', 'platform', 'activePlaylist', 'editingId', 'metadataGameId',
  'bigBoxIndex', 'gamepadState', 'lastSessionEvent', 'bulkMode',
  'bigBoxLastInput', 'screenSaverGame', 'contextGameId', 'availableProfiles',
  'appSettings', 'bigBoxFilter', 'bigBoxSort', 'bigBoxRaFilter',
  'bigBoxPlatform', 'platformCategory', 'pendingUpdate', 'duplicateMediaGroups',
  'libraryBgm', 'readerPage', 'readerUrl', 'bigBoxHybridQuery', 'mediaEpoch',
]);

function collectPatternNames(pattern, out) {
  if (!pattern) return;
  if (pattern.type === 'Identifier') { out.add(pattern.name); return; }
  if (pattern.type === 'AssignmentPattern') { collectPatternNames(pattern.left, out); return; }
  if (pattern.type === 'RestElement') { collectPatternNames(pattern.argument, out); return; }
  if (pattern.type === 'ObjectPattern') {
    for (const p of pattern.properties || []) {
      collectPatternNames(p.type === 'RestElement' ? p.argument : p.value, out);
    }
    return;
  }
  if (pattern.type === 'ArrayPattern') {
    for (const el of pattern.elements || []) collectPatternNames(el, out);
    return;
  }
}

function collectDeclared(bodyNode, out) {
  if (!bodyNode) return;
  const stack = [bodyNode];
  while (stack.length) {
    const node = stack.pop();
    if (!node || typeof node !== 'object') continue;
    if (['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression'].includes(node.type)) continue;
    if (node.type === 'VariableDeclaration') {
      for (const d of node.declarations || []) collectPatternNames(d.id, out);
    }
    for (const key of Object.keys(node)) {
      if (['type', 'start', 'end', 'range', 'loc'].includes(key)) continue;
      const child = node[key];
      if (Array.isArray(child)) for (const c of child) stack.push(c);
      else if (child && typeof child === 'object') stack.push(child);
    }
  }
}

const replacements = [];

function walkScope(root, shadow) {
  const stack = [root];
  const visited = new Set();
  while (stack.length) {
    const node = stack.pop();
    if (!node || typeof node !== 'object' || visited.has(node)) continue;
    visited.add(node);
    const parent = node._parent;

    if (node !== root && ['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression'].includes(node.type)) {
      const inner = new Set(shadow);
      for (const p of node.params || []) collectPatternNames(p, inner);
      if (node.id && node.id.name) inner.add(node.id.name);
      if (node.body && node.body.type === 'BlockStatement') collectDeclared(node.body, inner);
      walkScope(node.body, inner);
      continue;
    }

    if (node.type === 'Identifier' && OWNED.has(node.name) && !shadow.has(node.name)) {
      let skip = false;
      if (parent) {
        if (parent.type === 'MemberExpression' && !parent.computed && parent.property === node) skip = true;
        if (parent.type === 'Property' && parent.key === node && !parent.computed) skip = true;
        if (parent.type === 'VariableDeclarator' && parent.id === node) skip = true;
        if ((parent.type === 'FunctionDeclaration' || parent.type === 'FunctionExpression') && parent.id === node) skip = true;
        if (parent.type === 'LabeledStatement' && parent.label === node) skip = true;
        if ((parent.type === 'BreakStatement' || parent.type === 'ContinueStatement') && parent.label === node) skip = true;
      }
      if (!skip) replacements.push({ start: node.start, end: node.end, text: 'AppState.' + node.name });
    }

    if (node.type === 'Property' && node.shorthand && node.key && node.key.type === 'Identifier' &&
        OWNED.has(node.key.name) && !shadow.has(node.key.name)) {
      replacements.push({ start: node.start, end: node.end, text: `${node.key.name}: AppState.${node.key.name}` });
    }

    for (const key of Object.keys(node)) {
      if (['type', 'start', 'end', 'range', 'loc', '_parent'].includes(key)) continue;
      const child = node[key];
      if (Array.isArray(child)) {
        for (const c of child) {
          if (c && typeof c === 'object') { c._parent = node; stack.push(c); }
        }
      } else if (child && typeof child === 'object') {
        child._parent = node;
        stack.push(child);
      }
    }
  }
}

for (const st of ast.body) {
  if (['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression'].includes(st.type)) {
    const shadow = new Set();
    for (const p of st.params || []) collectPatternNames(p, shadow);
    if (st.id && st.id.name) shadow.add(st.id.name);
    if (st.body && st.body.type === 'BlockStatement') collectDeclared(st.body, shadow);
    walkScope(st.body, shadow);
  }
}

// Drop replacements nested inside another replacement (shorthand keys vs
// their own identifier nodes): outer spans win, inner spans are covered.
replacements.sort((a, b) => b.start - a.start);
const filtered = [];
for (const r of replacements) {
  const covered = filtered.some((f) => r.start >= f.start && r.end <= f.end && r !== f);
  if (!covered) filtered.push(r);
}
let out = src;
for (const r of filtered) {
  out = out.slice(0, r.start) + r.text + out.slice(r.end);
}
fs.writeFileSync('static/app.js', out);
console.log('read rewrites:', replacements.length);
