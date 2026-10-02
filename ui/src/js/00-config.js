// ── Config ───────────────────────────────────────────────────────────────────
const API_BASE  = 'http://127.0.0.1:8080';
// F-201: injected by the API when it serves this page on 127.0.0.1:8080
// (server.py ui()); never baked into the source.
const API_TOKEN = window.CLOUDCORE_API_TOKEN || '';
