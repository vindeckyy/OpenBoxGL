/**
 * worker.search.js — trigram expansion off main thread with fallback.
 * Shared trigram logic with main thread (state.js / util.js) to ensure identical results.
 * Protocol: postMessage {id, type:'search', query, games} -> postMessage {id, results, degraded}
 * Fallback: when Worker absent, library.js uses main-thread filteredGames()/index path directly.
 */

// Shared trigram helpers (mirrors static/util.js + static/state.js indexTerms)
const SEARCH_INDEX_MAX_TERM = 32;

function indexValues(game) {
  return [game.name, game.sort_title, ...(Array.isArray(game.alternate_names) ? game.alternate_names : [game.alternate_names])]
    .filter(v => v !== undefined && v !== null && v !== '')
    .map(v => String(v).toLowerCase());
}

function indexTerms(values) {
  const terms = new Set();
  values.forEach(value => {
    (value.match(/[a-z0-9]+/g) || []).forEach(word => {
      const limited = word.slice(0, SEARCH_INDEX_MAX_TERM);
      for (let end = 2; end <= Math.min(limited.length, 9); end++) terms.add(limited.slice(0, end));
      for (let len = 2; len <= Math.min(limited.length, 9); len++) terms.add(limited.slice(limited.length - len));
      for (let i = 0; i <= limited.length - 2; i++) terms.add(limited.slice(i, i + 2));
    });
    const words = value.match(/[a-z0-9]+/g) || [];
    if (words.length > 1) terms.add(words.map(w => w[0]).join(''));
    if (words.length > 2 && ['the', 'a', 'an'].includes(words[0])) terms.add(words.slice(1).map(w => w[0]).join(''));
  });
  return terms;
}

// Trigram expansion: for a query string, produce trigram set for fuzzy matching
function trigramsOf(value) {
  const s = String(value || '').toLowerCase();
  const out = new Set();
  // 3-gram sliding window
  for (let i = 0; i <= s.length - 3; i++) out.add(s.slice(i, i + 3));
  // also add bigrams for short queries (keeps parity with indexTerms bigram path)
  for (let i = 0; i <= s.length - 2; i++) out.add(s.slice(i, i + 2));
  return out;
}

function expandTrigrams(query) {
  const terms = new Set();
  const words = String(query || '').toLowerCase().match(/[a-z0-9]+/g) || [];
  words.forEach(word => {
    for (const tri of trigramsOf(word)) terms.add(tri);
    // keep original word prefix/suffix for indexedTitleCandidates parity
    const limited = word.slice(0, SEARCH_INDEX_MAX_TERM);
    for (let end = 2; end <= Math.min(limited.length, 9); end++) terms.add(limited.slice(0, end));
    for (let len = 2; len <= Math.min(limited.length, 9); len++) terms.add(limited.slice(limited.length - len));
  });
  // acronym expansion (e.g., "oot" -> "ocarina of time")
  if (words.length === 1 && words[0].length >= 2 && words[0].length <= 8) {
    terms.add(words[0]);
  }
  return [...terms];
}

// Build an in-worker search index (mirrors state.js buildSearchIndex)
let _workerIndex = { games: null, title: new Map(), all: [] };

function buildWorkerIndex(games) {
  if (_workerIndex.games === games) return _workerIndex;
  const title = new Map();
  games.forEach(game => {
    for (const term of indexTerms(indexValues(game))) {
      let bucket = title.get(term);
      if (!bucket) title.set(term, bucket = []);
      bucket.push(game);
    }
  });
  _workerIndex = { games, title, all: games };
  return _workerIndex;
}

function workerIndexedCandidates(query, games) {
  if (!query || /[:"]/.test(query)) return null;
  // minimal token parse: split on space, check for key:value
  const tokens = String(query).trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!tokens.length || tokens.some(t => t.includes(':') || t.startsWith('-') || !/^[a-z0-9]+$/.test(t) || t.length < 2 || t.length > 9)) return null;
  const idx = buildWorkerIndex(games);
  let ids = null;
  for (const tok of tokens) {
    const key = tok.slice(0, SEARCH_INDEX_MAX_TERM);
    if (!idx.title.has(key)) return null;
    const bucket = idx.title.get(key) || [];
    const next = new Set(bucket.map(g => g.id));
    ids = ids === null ? next : new Set([...ids].filter(id => next.has(id)));
    if (!ids.size) break;
  }
  return ids && ids.size ? idx.all.filter(g => ids.has(g.id)) : [];
}

// Main search entry used by both worker and fallback (ensures identical results)
function searchGames(games, query) {
  if (!query || !String(query).trim()) return games.slice();
  // Try indexed fast path first
  const candidates = workerIndexedCandidates(query, games);
  const source = candidates || games;
  // Fallback to full scan using trigram + substring (mirrors advancedQueryMatches)
  const q = String(query).trim().toLowerCase();
  const qTrigrams = trigramsOf(q);
  return source.filter(game => {
    const hay = [game.name, game.sort_title, ...(Array.isArray(game.alternate_names) ? game.alternate_names : [])].filter(Boolean).join(' ').toLowerCase();
    if (hay.includes(q)) return true;
    // trigram fuzzy: at least half trigrams match
    if (qTrigrams.size >= 3) {
      const hayTrigrams = trigramsOf(hay);
      let common = 0;
      for (const t of qTrigrams) if (hayTrigrams.has(t)) common++;
      if (common / qTrigrams.size >= 0.5) return true;
    }
    // acronym
    if (q.length >= 2 && q.length <= 8 && /^[a-z0-9]+$/i.test(q)) {
      const words = String(game.name || '').trim().match(/[A-Za-z0-9]+/g) || [];
      const acronym = words.map(w => w[0].toLowerCase()).join('');
      if (acronym === q || acronym.includes(q)) return true;
      if (words.length > 1 && ['the', 'a', 'an'].includes(words[0].toLowerCase())) {
        const sub = words.slice(1).map(w => w[0].toLowerCase()).join('');
        if (sub === q || sub.includes(q)) return true;
      }
    }
    return false;
  });
}

// Worker message protocol
self.onmessage = function(e) {
  const data = e.data || {};
  const id = data.id;
  try {
    if (data.type === 'expand') {
      const out = expandTrigrams(data.query || '');
      self.postMessage({ id, type: 'expand', trigrams: out });
    } else if (data.type === 'search') {
      const games = Array.isArray(data.games) ? data.games : [];
      const query = String(data.query || '');
      const results = searchGames(games, query);
      self.postMessage({ id, type: 'search', results, count: results.length });
    } else if (data.type === 'warm') {
      const games = Array.isArray(data.games) ? data.games : [];
      buildWorkerIndex(games);
      self.postMessage({ id, type: 'warm', ok: true });
    } else {
      self.postMessage({ id, error: 'unknown type' });
    }
  } catch (err) {
    self.postMessage({ id, error: String(err && err.message || err) });
  }
};

// Export for main-thread fallback testing (when imported as module)
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { trigramsOf, expandTrigrams, searchGames, buildWorkerIndex, workerIndexedCandidates, indexTerms, indexValues };
}
