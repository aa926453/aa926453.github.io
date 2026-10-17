// Cloudflare Worker - 源托管 + 下载代理
// 部署前需要设置环境变量:
//   CODES      : JSON 数组,激活码列表(未设置则下载不校验)
//   WORKER_URL : 本 Worker 的完整 URL,如 https://xxx.workers.dev
// 需要绑定 KV 命名空间: APPSTORE_KV
// 源文件(appstore.json)托管在 GitHub Pages,本 Worker 拉取并对外提供访问,
// 下载链接经 /dl 代理 302 重定向到真实直链,隐藏上游域名。

const UPSTREAM_APPSTORE = 'https://aa926453.github.io/appstore.json';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // 源文件:从 GitHub Pages 拉取并返回(对外隐藏真实地址)
    if (path === '/appstore.json' || path === '/appstore') {
      return handleAppstore(request, env, ctx);
    }

    // 下载代理:302 重定向到真实直链(无激活码时不校验)
    if (path === '/dl') {
      return handleDownload(request, env, url);
    }

    // 激活页(可选,未配置 CODES 时不启用)
    if (path === '/' || path === '/activate') {
      return new Response(ACTIVATE_HTML, {
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      });
    }

    // 激活接口
    if (path === '/api/activate' && request.method === 'POST') {
      return handleActivate(request, env);
    }

    // 免输入续期接口(换网后打开页面自动调用)
    if (path === '/api/refresh' && request.method === 'POST') {
      return handleRefresh(request, env);
    }

    // 查询当前 IP 激活状态
    if (path === '/api/status') {
      return handleStatus(request, env);
    }

    return new Response('Not Found', { status: 404 });
  },
};

/* ---------- 源文件托管 ---------- */

async function handleAppstore(request, env, ctx) {
  const kv = env.APPSTORE_KV;
  // 优先读 KV 缓存(60 秒),减少回源
  const cached = await kv.get('appstore:body');
  if (cached) {
    return new Response(cached, {
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'public, max-age=60',
      },
    });
  }
  // 回源拉取 GitHub Pages
  try {
    const resp = await fetch(UPSTREAM_APPSTORE, {
      headers: { 'User-Agent': 'iosipa-worker/1.0' },
      cf: { cacheTtl: 30, cacheEverything: true },
    });
    if (!resp.ok) throw new Error('upstream ' + resp.status);
    const body = await resp.text();
    // 写入 KV 缓存(异步,不阻塞响应)
    ctx.waitUntil(kv.put('appstore:body', body, { expirationTtl: 120 }));
    return new Response(body, {
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'public, max-age=60',
      },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: 'upstream failed', detail: String(e) }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

/* ---------- 工具函数 ---------- */

function getClientIP(request) {
  return (
    request.headers.get('cf-connecting-ip') ||
    (request.headers.get('x-forwarded-for') || '').split(',')[0].trim() ||
    '0.0.0.0'
  );
}

function parseCodes(raw) {
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

/* ---------- 下载鉴权 ---------- */

async function handleDownload(request, env, url) {
  const target = url.searchParams.get('url');
  if (!target) {
    return new Response('Missing url', { status: 400 });
  }

  // 未配置 CODES 时,直接放行(暂不启用激活功能)
  const codes = parseCodes(env.CODES);
  if (codes.length === 0) {
    return Response.redirect(target, 302);
  }

  const ip = getClientIP(request);
  const kv = env.APPSTORE_KV;

  const ipData = await kv.get(`ip:${ip}`, 'json');

  if (!ipData) {
    return json(
      {
        error: 'not_activated',
        message: `未激活,请先访问 ${env.WORKER_URL}/ 激活`,
      },
      403
    );
  }

  if (ipData.expiresAt && Date.now() > ipData.expiresAt) {
    await kv.delete(`ip:${ip}`);
    return json(
      {
        error: 'expired',
        message: `激活已过期,请重新访问 ${env.WORKER_URL}/ 激活`,
      },
      403
    );
  }

  // 已激活 -> 302 重定向到真实直链
  return Response.redirect(target, 302);
}

/* ---------- 激活 ---------- */

async function handleActivate(request, env) {
  const body = await request.json().catch(() => ({}));
  const code = (body.code || '').trim();
  const fingerprint = body.fingerprint || '';
  const ip = getClientIP(request);
  const kv = env.APPSTORE_KV;

  if (!code) {
    return json({ ok: false, message: '请输入激活码' }, 400);
  }

  const codes = parseCodes(env.CODES);
  const codeInfo = codes.find((c) => c.code === code);
  if (!codeInfo) {
    return json({ ok: false, message: '激活码无效' }, 403);
  }

  // 过期检查
  if (codeInfo.expires) {
    const exp = new Date(codeInfo.expires).getTime();
    if (Date.now() > exp) {
      return json({ ok: false, message: '激活码已过期' }, 403);
    }
  }

  // 一机一码:该激活码是否已绑定其他设备
  const codeKey = `code:${code}`;
  const used = await kv.get(codeKey, 'json');
  if (used && used.fingerprint && fingerprint && used.fingerprint !== fingerprint) {
    return json({ ok: false, message: '该激活码已绑定其他设备' }, 403);
  }

  const now = Date.now();
  const expiresAt = codeInfo.expires
    ? new Date(codeInfo.expires).getTime()
    : now + 30 * 86400000; // 默认 30 天

  const record = { fingerprint, ip, activatedAt: now, expiresAt };
  await kv.put(codeKey, JSON.stringify(record));
  if (fingerprint) {
    await kv.put(`fp:${fingerprint}`, JSON.stringify(record));
  }
  await kv.put(`ip:${ip}`, JSON.stringify(record));

  return json({ ok: true, message: '激活成功!返回全能签即可下载' });
}

/* ---------- 免输入续期(换网后打开页面自动调用) ---------- */

async function handleRefresh(request, env) {
  const body = await request.json().catch(() => ({}));
  const fingerprint = body.fingerprint || '';
  const ip = getClientIP(request);
  const kv = env.APPSTORE_KV;

  if (!fingerprint) {
    return json({ ok: false, message: '缺少设备信息' }, 400);
  }

  const fpData = await kv.get(`fp:${fingerprint}`, 'json');
  if (!fpData) {
    return json({ ok: false, message: '该设备未激活,请输入激活码' }, 403);
  }

  if (fpData.expiresAt && Date.now() > fpData.expiresAt) {
    await kv.delete(`fp:${fingerprint}`);
    return json({ ok: false, message: '激活已过期,请重新输入激活码' }, 403);
  }

  // 更新 IP 绑定
  await kv.put(
    `ip:${ip}`,
    JSON.stringify({
      fingerprint,
      code: fpData.code,
      activatedAt: fpData.activatedAt,
      expiresAt: fpData.expiresAt,
    })
  );

  return json({ ok: true, message: '已自动续期,返回全能签即可下载' });
}

/* ---------- 状态查询 ---------- */

async function handleStatus(request, env) {
  const ip = getClientIP(request);
  const kv = env.APPSTORE_KV;
  const data = await kv.get(`ip:${ip}`, 'json');
  return json({
    activated: !!data,
    expiresAt: data ? data.expiresAt : null,
  });
}

/* ---------- 激活页 HTML ---------- */

const ACTIVATE_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>激活下载权限</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; margin: 0; padding: 16px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 32px 28px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.2);
    width: 100%; max-width: 420px;
  }
  h1 { font-size: 24px; margin: 0 0 8px; color: #1a1a1a; }
  .sub { color: #888; margin: 0 0 28px; font-size: 14px; line-height: 1.6; }
  input {
    width: 100%; padding: 16px; border: 2px solid #e5e5ea;
    border-radius: 12px; font-size: 17px; outline: none;
    transition: border-color .2s;
  }
  input:focus { border-color: #007aff; }
  button {
    width: 100%; padding: 16px; background: #007aff; color: #fff;
    border: none; border-radius: 12px; font-size: 17px; font-weight: 600;
    margin-top: 16px; cursor: pointer; transition: opacity .2s;
  }
  button:disabled { background: #c7c7cc; cursor: not-allowed; }
  .msg { margin-top: 18px; padding: 14px; border-radius: 10px; font-size: 14px; line-height: 1.5; }
  .ok { background: #e8f5e9; color: #2e7d32; }
  .err { background: #ffebee; color: #c62828; }
  .tip { margin-top: 20px; padding-top: 20px; border-top: 1px solid #f0f0f0; font-size: 12px; color: #aaa; line-height: 1.7; }
</style>
</head>
<body>
<div class="card">
  <h1>激活下载权限</h1>
  <p class="sub">输入激活码后,即可下载本源全部应用<br>换网络后重新打开本页可自动续期</p>
  <input id="code" type="text" placeholder="请输入激活码" autocomplete="off">
  <button id="btn">立即激活</button>
  <div id="msg"></div>
  <div class="tip">
    激活码由源主分发,如无激活码请联系源主获取。<br>
    一个激活码仅限一台设备使用。
  </div>
</div>
<script>
// 简单设备指纹(UA + 语言 + 屏幕 + 时区)
function getFingerprint() {
  var info = [
    navigator.userAgent,
    navigator.language,
    screen.width + 'x' + screen.height + 'x' + screen.colorDepth,
    new Date().getTimezoneOffset()
  ].join('|');
  var hash = 0;
  for (var i = 0; i < info.length; i++) {
    hash = ((hash << 5) - hash) + info.charCodeAt(i);
    hash |= 0;
  }
  return 'fp_' + Math.abs(hash).toString(36);
}

var fp = getFingerprint();
var msg = document.getElementById('msg');
var btn = document.getElementById('btn');
var codeInput = document.getElementById('code');

function showMsg(text, type) {
  msg.className = 'msg ' + (type || '');
  msg.textContent = text;
}

// 页面加载时尝试免输入续期
fetch('/api/refresh', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ fingerprint: fp })
}).then(function(r){ return r.json(); }).then(function(d){
  if (d.ok) { showMsg(d.message, 'ok'); btn.disabled = true; btn.textContent = '已激活'; }
}).catch(function(){});

btn.onclick = function() {
  var code = codeInput.value.trim();
  if (!code) { showMsg('请输入激活码', 'err'); return; }
  btn.disabled = true;
  btn.textContent = '激活中...';
  fetch('/api/activate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: code, fingerprint: fp })
  }).then(function(r){ return r.json(); }).then(function(d){
    if (d.ok) { showMsg(d.message, 'ok'); btn.textContent = '已激活'; }
    else { showMsg(d.message, 'err'); btn.disabled = false; btn.textContent = '立即激活'; }
  }).catch(function(){
    showMsg('网络错误,请重试', 'err');
    btn.disabled = false; btn.textContent = '立即激活';
  });
};
</script>
</body>
</html>`;
