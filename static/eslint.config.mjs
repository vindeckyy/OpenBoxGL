// Minimal flat-config lint for the frontend modules (static/*.js).
// Syntax + no-unsanitized/method only — no stylistic rules.
// Run from the repo root: eslint --config static/eslint.config.mjs static/
// (flat-config base path is the cwd, so the files glob is root-relative).
import globals from '../scripts/node_modules/globals/index.js';

export default [
  {
    files: ['static/**/*.js', '*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.browser,
        // AppState is a module-level binding imported by each file, but the
        // top-level entry (index.html) references it as a global after the
        // module graph loads, so keep it known here.
        AppState: 'readonly',
      },
    },
    rules: {
      'no-unsanitized/method': 'off',
    },
  },
];
