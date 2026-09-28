// net_guard.cjs — proxy LỌC MẠNG cho trình duyệt do NGƯỜI LẠ điều khiển (skill công khai
// browser.remote, user 28/9/2026: «share browser mình sở hữu public»).
//
// Vì sao phải có: trình duyệt chạy TRÊN MÁY CHỦ. Không có lớp này, người điều khiển gõ
// http://127.0.0.1:5295/terminal là vào dashboard với quyền CHỦ (loopback không qua proxy
// = chủ, core/auth.py), mở được cổng CDP của mọi trình duyệt khác, metadata cloud
// 169.254.169.254, router nhà trong LAN…
//
// Cách làm: Chromium được ép đi qua proxy này (--proxy-server + Playwright tự thêm
// <-loopback> nên CẢ 127.0.0.1 cũng phải đi qua). Proxy tự phân giải DNS, từ chối nếu BẤT KỲ
// địa chỉ nào là nội bộ/đặc biệt, rồi kết nối vào ĐÚNG IP vừa kiểm — tên miền trỏ về
// 127.0.0.1 hay DNS rebinding (đổi IP giữa lúc kiểm và lúc nối) đều không lách được, vì
// Chromium không bao giờ tự nối. TLS vẫn đầu-cuối (CONNECT chỉ chọn đích TCP; SNI giữ tên).
// Hồ sơ có proxy riêng → nối chuỗi qua proxy đó (http/https có mật khẩu, socks5 không mật
// khẩu), vẫn gửi IP đã kiểm chứ không gửi tên.
'use strict';

const http = require('http');
const net = require('net');
const tls = require('tls');
const dns = require('dns').promises;

const ALLOWED_PORTS = new Set([80, 443, 8080, 8443]);
const CONNECT_TIMEOUT_MS = 15000;

function v4Private(ip) {
    const p = ip.split('.').map(Number);
    if (p.length !== 4 || p.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return true;
    const [a, b, c] = p;
    return a === 0 || a === 10 || a === 127 || a >= 224                     // this-net, private, loopback, multicast+reserved
        || (a === 100 && b >= 64 && b <= 127)                               // CGNAT
        || (a === 169 && b === 254)                                         // link-local + metadata cloud
        || (a === 172 && b >= 16 && b <= 31)
        || (a === 192 && b === 168)
        || (a === 192 && b === 0 && (c === 0 || c === 2))                   // IETF, TEST-NET-1
        || (a === 192 && b === 88 && c === 99)                              // 6to4 relay
        || (a === 198 && (b === 18 || b === 19))                            // benchmark
        || (a === 198 && b === 51 && c === 100) || (a === 203 && b === 0 && c === 113);
}

function expandV6(ip) {
    let s = ip.toLowerCase().split('%')[0];
    const v4 = s.match(/(\d+\.\d+\.\d+\.\d+)$/);
    let tail = [];
    if (v4) {
        const p = v4[1].split('.').map(Number);
        tail = [((p[0] << 8) | p[1]).toString(16), ((p[2] << 8) | p[3]).toString(16)];
        s = s.slice(0, -v4[1].length).replace(/:$/, '') || ':';
        if (s.endsWith(':') && !s.endsWith('::')) s = s.slice(0, -1);
    }
    const [head, rest] = s.includes('::') ? s.split('::') : [s, null];
    const h = head ? head.split(':').filter(Boolean) : [];
    const r = rest !== null ? (rest ? rest.split(':').filter(Boolean) : []) : [];
    const fill = rest !== null ? 8 - h.length - r.length - tail.length : 0;
    const groups = [...h, ...Array(Math.max(0, fill)).fill('0'), ...r, ...tail];
    return groups.length === 8 ? groups.map((g) => parseInt(g, 16)) : null;
}

function v6Private(ip) {
    const g = expandV6(ip);
    if (!g || g.some((n) => Number.isNaN(n))) return true;
    const embedded = (hi, lo) => `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
    if (g.every((n) => n === 0)) return true;                                 // ::
    if (g.slice(0, 7).every((n) => n === 0) && g[7] === 1) return true;       // ::1
    if (g.slice(0, 5).every((n) => n === 0) && g[5] === 0xffff) return v4Private(embedded(g[6], g[7]));  // ::ffff:a.b.c.d
    if (g.slice(0, 6).every((n) => n === 0)) return v4Private(embedded(g[6], g[7]));                     // ::a.b.c.d cũ
    if (g[0] === 0x64 && g[1] === 0xff9b) return v4Private(embedded(g[6], g[7]));                       // NAT64
    if (g[0] === 0x2002) return v4Private(embedded(g[1], g[2]));                                        // 6to4
    return (g[0] & 0xfe00) === 0xfc00 || (g[0] & 0xffc0) === 0xfe80 || (g[0] & 0xff00) === 0xff00
        || g[0] === 0x2001 && g[1] === 0x0db8;                                                          // ULA, link-local, multicast, doc
}

function isPrivateAddress(ip) {
    const kind = net.isIP(ip);
    if (kind === 4) return v4Private(ip);
    if (kind === 6) return v6Private(ip);
    return true;
}

// Tên chắc chắn nội bộ — chặn luôn, khỏi hỏi DNS.
const LOCAL_NAME_RE = /(^|\.)(localhost|local|internal|intranet|lan|home\.arpa|localdomain)$/i;

async function resolvePublic(host) {
    const h = String(host || '').replace(/^\[|\]$/g, '').replace(/\.$/, '');
    if (!h) return null;
    if (net.isIP(h)) return isPrivateAddress(h) ? null : h;
    if (LOCAL_NAME_RE.test(h) || !h.includes('.')) return null;
    let addrs;
    try {
        addrs = await dns.lookup(h, { all: true, verbatim: true });
    } catch (e) {
        return null;
    }
    // MỘT địa chỉ nội bộ trong danh sách là đủ để từ chối (tên trả cả IP công khai lẫn 127.0.0.1).
    if (!addrs.length || addrs.some((a) => isPrivateAddress(a.address))) return null;
    return (addrs.find((a) => a.family === 4) || addrs[0]).address;
}

function splitHostPort(authority, dflt) {
    const m = String(authority || '').match(/^\[([^\]]+)\](?::(\d+))?$|^([^:]+)(?::(\d+))?$/);
    if (!m) return null;
    const host = m[1] || m[3];
    const port = Number(m[2] || m[4] || dflt);
    return Number.isInteger(port) && port > 0 && port < 65536 ? { host, port } : null;
}

function readUntil(sock, marker, limit = 16384) {
    return new Promise((resolve, reject) => {
        let buf = Buffer.alloc(0);
        const onData = (d) => {
            buf = Buffer.concat([buf, d]);
            const i = buf.indexOf(marker);
            if (i >= 0) {
                sock.removeListener('data', onData);
                sock.removeListener('error', reject);
                const rest = buf.slice(i + marker.length);
                if (rest.length) sock.unshift(rest);
                resolve(buf.slice(0, i));
            } else if (buf.length > limit) {
                reject(new Error('upstream reply too large'));
            }
        };
        sock.on('data', onData);
        sock.once('error', reject);
    });
}

function readBytes(sock, n) {
    return new Promise((resolve, reject) => {
        let buf = Buffer.alloc(0);
        const onData = (d) => {
            buf = Buffer.concat([buf, d]);
            if (buf.length >= n) {
                sock.removeListener('data', onData);
                sock.removeListener('error', reject);
                if (buf.length > n) sock.unshift(buf.slice(n));
                resolve(buf.slice(0, n));
            }
        };
        sock.on('data', onData);
        sock.once('error', reject);
    });
}

function rawConnect(host, port, useTls) {
    return new Promise((resolve, reject) => {
        const s = useTls ? tls.connect({ host, port, servername: net.isIP(host) ? undefined : host })
                         : net.connect({ host, port });
        const t = setTimeout(() => { s.destroy(); reject(new Error('connect timeout')); }, CONNECT_TIMEOUT_MS);
        s.once(useTls ? 'secureConnect' : 'connect', () => { clearTimeout(t); resolve(s); });
        s.on('error', (e) => { clearTimeout(t); reject(e); });
    });
}

// Socket tới ip:port — thẳng, hoặc qua proxy của hồ sơ (vẫn gửi IP đã kiểm).
async function openTunnel(ip, port, upstream) {
    if (!upstream) return rawConnect(ip, port, false);
    const target = net.isIP(ip) === 6 ? `[${ip}]:${port}` : `${ip}:${port}`;
    if (upstream.scheme === 'socks5') {
        const s = await rawConnect(upstream.host, upstream.port, false);
        s.write(Buffer.from([5, 1, 0]));
        const hello = await readBytes(s, 2);
        if (hello[0] !== 5 || hello[1] !== 0) { s.destroy(); throw new Error('socks5 auth refused'); }
        const kind = net.isIP(ip);
        const addr = kind === 4 ? Buffer.from([1, ...ip.split('.').map(Number)])
            : Buffer.concat([Buffer.from([4]), Buffer.from(expandV6(ip).flatMap((n) => [n >> 8, n & 255]))]);
        s.write(Buffer.concat([Buffer.from([5, 1, 0]), addr, Buffer.from([port >> 8, port & 255])]));
        const head = await readBytes(s, 4);
        if (head[1] !== 0) { s.destroy(); throw new Error('socks5 connect failed ' + head[1]); }
        const alen = head[3] === 1 ? 4 : head[3] === 4 ? 16 : (await readBytes(s, 1))[0];
        await readBytes(s, alen + 2);
        return s;
    }
    const s = await rawConnect(upstream.host, upstream.port, upstream.scheme === 'https');
    let req = `CONNECT ${target} HTTP/1.1\r\nHost: ${target}\r\n`;
    if (upstream.username) {
        const cred = Buffer.from(`${upstream.username}:${upstream.password || ''}`).toString('base64');
        req += `Proxy-Authorization: Basic ${cred}\r\n`;
    }
    s.write(req + '\r\n');
    const reply = (await readUntil(s, '\r\n\r\n')).toString('latin1');
    if (!/^HTTP\/1\.[01] 200/.test(reply)) { s.destroy(); throw new Error('upstream refused: ' + reply.split('\r\n')[0]); }
    return s;
}

function refuse(sock, code, why) {
    try { sock.end(`HTTP/1.1 ${code} ${why}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nBlocked by TubeCLI: ${why}\r\n`); } catch (e) {}
}

/**
 * startNetGuard({ upstream, log }) → { port, close() }
 * upstream: null | { scheme: 'http'|'https'|'socks5', host, port, username?, password? }
 */
function startNetGuard({ upstream = null, log = () => {} } = {}) {
    const server = http.createServer();

    server.on('connect', async (req, client, head) => {
        // Trình duyệt huỷ kết nối giữa chừng là chuyện thường — lỗi socket KHÔNG được làm
        // sập proxy (sập proxy = sập cả phiên trình duyệt của người xem).
        client.on('error', () => {});
        const t = splitHostPort(req.url, 443);
        if (!t || !ALLOWED_PORTS.has(t.port)) { log(`[net_guard] chặn cổng ${req.url}`); return refuse(client, 403, 'port not allowed'); }
        const ip = await resolvePublic(t.host);
        if (!ip) { log(`[net_guard] chặn đích nội bộ ${t.host}`); return refuse(client, 403, 'private address'); }
        let up;
        try { up = await openTunnel(ip, t.port, upstream); } catch (e) { return refuse(client, 502, 'connect failed'); }
        up.on('error', () => {});
        if (client.destroyed) { up.destroy(); return; }
        client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        if (head && head.length) up.write(head);
        up.pipe(client); client.pipe(up);
        const done = () => { up.destroy(); client.destroy(); };
        up.on('error', done); client.on('error', done); up.on('close', done); client.on('close', done);
    });

    server.on('request', async (req, res) => {
        req.on('error', () => {}); res.on('error', () => {});
        let u;
        try { u = new URL(req.url); } catch (e) { res.writeHead(400).end(); return; }
        if (u.protocol !== 'http:') { res.writeHead(403).end('Blocked by TubeCLI: scheme'); return; }
        const port = Number(u.port || 80);
        if (!ALLOWED_PORTS.has(port)) { res.writeHead(403).end('Blocked by TubeCLI: port not allowed'); return; }
        const ip = await resolvePublic(u.hostname);
        if (!ip) { log(`[net_guard] chặn đích nội bộ ${u.hostname}`); res.writeHead(403).end('Blocked by TubeCLI: private address'); return; }
        let sock;
        try { sock = await openTunnel(ip, port, upstream); } catch (e) { res.writeHead(502).end(); return; }
        sock.on('error', () => {});
        const headers = { ...req.headers };
        for (const k of Object.keys(headers)) if (/^proxy-/i.test(k)) delete headers[k];
        headers.connection = 'close';
        const out = http.request({ method: req.method, path: u.pathname + u.search, headers,
                                   createConnection: () => sock }, (pr) => {
            res.writeHead(pr.statusCode || 502, pr.headers);
            pr.pipe(res);
        });
        out.on('error', () => { try { res.writeHead(502).end(); } catch (e) {} });
        req.pipe(out);
    });

    server.on('clientError', (e, sock) => { try { sock.destroy(); } catch (x) {} });
    return new Promise((resolve, reject) => {
        server.listen(0, '127.0.0.1', () => {
            const port = server.address().port;
            log(`[net_guard] proxy lọc mạng tại 127.0.0.1:${port}` + (upstream ? ` → ${upstream.scheme}://${upstream.host}:${upstream.port}` : ''));
            resolve({ port, close: () => new Promise((r) => server.close(() => r())) });
        });
        server.once('error', reject);
    });
}

// URL mà NGƯỜI ĐIỀU KHIỂN được yêu cầu mở (lệnh navigate/new_tab/startUrl). Mạng đã có
// proxy lọc; lớp này chặn các scheme KHÔNG đi mạng: file:, chrome:, devtools:, view-source:…
function isAllowedNavigation(url) {
    const s = String(url || '').trim();
    if (!s || s === 'about:blank') return true;
    let u;
    try { u = new URL(s); } catch (e) { return false; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    if (u.username || u.password) return false;
    const h = u.hostname.replace(/^\[|\]$/g, '');
    if (net.isIP(h) && isPrivateAddress(h)) return false;
    return !(LOCAL_NAME_RE.test(h) || !h.includes('.'));
}

module.exports = { startNetGuard, isPrivateAddress, resolvePublic, isAllowedNavigation, splitHostPort, ALLOWED_PORTS };
