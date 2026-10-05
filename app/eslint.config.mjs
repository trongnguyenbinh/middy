// Lint for the Electron shell: main + preload are CommonJS on Node, the renderer is ES modules + JSX in Chromium.
// Correctness rules only (eslint recommended + React hooks); no formatting rules.
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'

export default [
  { ignores: ['dist/**', 'release/**', 'release_*/**', 'node_modules/**', 'build/**'] },
  js.configs.recommended,
  {
    rules: {
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]', argsIgnorePattern: '^_', caughtErrors: 'none' }],   // ^[A-Z]: components used only in JSX
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
  {
    files: ['main/**/*.js', 'preload/**/*.js', 'tools/**/*.js', 'test/**/*.js'],
    languageOptions: { sourceType: 'commonjs', globals: { ...globals.node } },
  },
  {
    files: ['renderer/**/*.{js,jsx,mjs}'],
    languageOptions: { sourceType: 'module', parserOptions: { ecmaFeatures: { jsx: true } }, globals: { ...globals.browser } },
    plugins: { 'react-hooks': reactHooks },
    rules: { 'react-hooks/rules-of-hooks': 'error' },
  },
  {
    files: ['renderer/worklets/**/*.js'],
    languageOptions: { sourceType: 'script', globals: { ...globals.browser, ...globals.audioWorklet, WebRtcAec3: 'readonly', __AEC_WASM_B64: 'readonly' } },   // both defined by the bundle build.mjs writes
  },
  {
    files: ['preload/index.js'],
    languageOptions: { globals: { ...globals.browser } },
  },
  {
    files: ['*.mjs', 'tools/**/*.mjs', 'test/**/*.mjs'],
    languageOptions: { sourceType: 'module', globals: { ...globals.node } },
  },
]
