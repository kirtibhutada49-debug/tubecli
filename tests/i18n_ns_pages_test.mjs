/**
 * Mỗi trang tĩnh chỉ xin đúng nhóm khoá của nó (window.I18N_NS → /api/v1/i18n/{lang}?ns=…).
 *
 * Run:  node tests/i18n_ns_pages_test.mjs     (exit 0 = pass)
 *
 * Vì sao (3/10/2026): trọn bộ 31 extension ≈ 230 KB, mọi trang đều tải trọn bộ dù chỉ dùng
 * một nhóm (codex ≈ 8 %). Dashboard SPA (webui/static/index.html + app.js) nhúng nhiều panel
 * nên VẪN tải trọn bộ — cố ý, không nằm trong danh sách này.
 *
 * Kiểm bằng quét tĩnh: với mỗi trang khai I18N_NS, gom HTML + mọi script cục bộ nó nạp, lấy
 * đoạn đầu của mọi khoá chữ (T('x.…'), t('x.…'), data-i18n="x.…") → phải nằm trong I18N_NS;
 * và mỗi ns khai phải có thật trong locales/en.json của một extension (gõ sai ns = trang mất chữ).
 * Thêm: route gộp đặt Cache-Control stale-while-revalidate (trình duyệt vẽ ngay bản đệm).
 */
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const EXT = path.join(ROOT, 'tubecli', 'extensions');
const read = (p) => fs.readFileSync(p, 'utf8');

const PAGES = [
  'codex/static/codex.html', 'chat/static/chat.html', 'browser_scripts/static/index.html', 'video_editor/static/editor.html',
  'webui/static/auth_manager.html', 'webui/static/browser_view.html', 'webui/static/market.html', 'webui/static/story.html',
  'webui/static/studio.html', 'webui/static/teams.html', 'webui/static/workflow.html',
];
// URL tĩnh → thư mục trên đĩa (như server mount)
const MOUNT = { '/static/': 'webui/static/', '/codex/': 'codex/static/', '/chat/': 'chat/static/', '/video-editor-static/': 'video_editor/static/' };
const local = (src) => {
  const s = src.split('?')[0];
  for (const [u, d] of Object.entries(MOUNT)) if (s.startsWith(u)) return path.join(EXT, d, s.slice(u.length));
  return null;
};

// Mọi ns có thật (đoạn đầu khoá) trong en.json của extension built-in + external đã cài
const nsAll = new Set();
const roots = [EXT, path.join(ROOT, 'data', 'extensions_external')];
for (const r of roots) {
  if (!fs.existsSync(r)) continue;
  for (const e of fs.readdirSync(r)) {
    const f = path.join(r, e, 'locales', 'en.json');
    if (!fs.existsSync(f)) continue;
    try { for (const k of Object.keys(JSON.parse(read(f)))) nsAll.add(k.split('.')[0]); } catch {}
  }
}
assert(nsAll.has('codex') && nsAll.has('common'), 'bộ en.json phải có codex + common');

const KEY = String.raw`['"\`]([a-zA-Z_][a-zA-Z0-9_]*)\.[a-zA-Z0-9_.{}$-]*['"\`]`;
const reCall = new RegExp(String.raw`\b(?:T|t|i18n|tr|tt|_t)\(\s*` + KEY, 'g');
const reAttr = /data-i18n(?:-placeholder|-title|-ph)?="([a-zA-Z_][a-zA-Z0-9_]*)\./g;

let n = 0;
for (const rel of PAGES) {
  const html = path.join(EXT, rel);
  const src = read(html);
  const m = /window\.I18N_NS\s*=\s*(\[[^\]]*\]);/.exec(src);
  assert(m, `${rel}: thiếu window.I18N_NS`);
  const ns = JSON.parse(m[1].replace(/'/g, '"'));
  assert(Array.isArray(ns) && ns.length, `${rel}: I18N_NS rỗng`);
  // i18n.js đọc window.I18N_NS lúc GỌI loadI18nFromApi, nên chỉ cần đứng trước lời gọi inline (nếu có)
  const call = src.indexOf('loadI18nFromApi(');
  assert(call < 0 || src.indexOf('window.I18N_NS') < call, `${rel}: I18N_NS phải đứng TRƯỚC lời gọi loadI18nFromApi trong HTML`);
  for (const x of ns) assert(nsAll.has(x), `${rel}: ns «${x}» không có trong en.json nào`);
  // gom nguồn: HTML + script cục bộ (trừ i18n.js)
  const files = [html];
  for (const s of src.matchAll(/<script[^>]*src="([^"]+)"/g)) {
    const p = local(s[1]);
    if (p && fs.existsSync(p) && !p.endsWith('i18n.js')) files.push(p);
  }
  const used = new Set();
  for (const f of files) {
    const c = read(f);
    for (const x of c.matchAll(reCall)) used.add(x[1]);
    for (const x of c.matchAll(reAttr)) used.add(x[1]);
  }
  // chỉ xét đoạn đầu CÓ THẬT trong bộ en.json — 'tab', 'nav'… của video_editor là khoá đã lệch
  // tiền tố (locale là veditor.nav.*, markup gọi nav.*) và chưa bao giờ dịch được: không tính.
  const missing = [...used].filter((x) => nsAll.has(x) && !ns.includes(x));
  assert.deepStrictEqual(missing, [], `${rel}: dùng ns ${JSON.stringify(missing)} mà I18N_NS chỉ có ${JSON.stringify(ns)}`);
  n++;
}
console.log(`1 trang     : ${n} trang khai I18N_NS đúng trước i18n.js, ns có thật, phủ hết khoá dùng`);

const spa = read(path.join(EXT, 'webui', 'static', 'index.html'));
assert(!/window\.I18N_NS/.test(spa), 'dashboard SPA cố ý KHÔNG khai ns (nhúng nhiều panel)');
const server = read(path.join(ROOT, 'tubecli', 'api', 'server.py'));
assert(server.includes('I18N_CACHE_CONTROL = "max-age=0, stale-while-revalidate=86400"'), 'route gộp: stale-while-revalidate');
assert(server.includes('"Cache-Control": I18N_CACHE_CONTROL'), 'route gộp dùng hằng');
const i18n = read(path.join(EXT, 'webui', 'static', 'i18n.js'));
assert(i18n.includes('window.I18N_NS'), 'i18n.js đọc window.I18N_NS');
console.log('2 may chu   : SPA trọn bộ | Cache-Control stale-while-revalidate | i18n.js đọc I18N_NS');
console.log('OK i18n_ns_pages_test');
