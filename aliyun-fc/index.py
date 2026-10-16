# -*- coding: utf-8 -*-
"""全能签聚合源 - 阿里云函数计算 FC 3.0 (python3.10)
实时聚合 5 个上游源，10 分钟进程内缓存，兼容 WSGI(HTTP触发) 与事件调用

部署信息（恢复/修改时用）:
  账号ID:   1518158992643781 (AccessKey 由用户提供, CLI 已配置在 ~/.aliyun)
  区域:     cn-hangzhou
  函数名:   appstore-agg  (runtime python3.10, handler index.handler, 1024MB, timeout 120s)
  触发器:   http (匿名, GET/HEAD)
  线上地址: https://appstore-agg-qeakfulqpj.cn-hangzhou.fcapp.run/appstore
  更新代码: zip 后 PUT /2023-03-30/functions/appstore-agg
            (endpoint: 1518158992643781.cn-hangzhou.fc.aliyuncs.com)
  不使用不产生费用 (最小实例数=0, 按量计费)
"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES = [
    'https://ipa.hxys2.xyz/appstore/',
    'https://app.dumpapps.com/appstore',
    'https://ipa.312752.cn/appstore',
    'https://qnq.nuosike.cn/appstore',
    'https://app.iosrxwy.com/appstore',
]
UA = 'OmniSign/7.7'
CACHE_TTL = 600
SOURCE_URL = 'https://appstore-agg-qeakfulqpj.cn-hangzhou.fcapp.run/appstore'
KEEP_FIELDS = ('name', 'bundleIdentifier', 'version', 'versionDate', 'iconURL',
               'downloadURL', 'size', 'tintColor', 'type')
MAX_STR = 300

_cache = {'ts': 0.0, 'body': b''}


def truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes')


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def slim(app):
    out = {}
    for k in KEEP_FIELDS:
        v = app.get(k)
        if v in (None, ''):
            continue
        if isinstance(v, str) and len(v) > MAX_STR:
            v = v[:MAX_STR]
        out[k] = v
    desc = app.get('versionDescription') or app.get('localizedDescription') or ''
    if desc:
        out['versionDescription'] = desc[:MAX_STR]
    return out


def aggregate():
    docs = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = [ex.submit(fetch, u) for u in SOURCES]
        for f in futs:
            try:
                docs.append(f.result())
            except Exception:
                pass
    apps, seen = [], set()
    for d in docs:
        for a in d.get('apps', []):
            if truthy(a.get('lock', 0)) or truthy(a.get('isLanZouCloud', 0)):
                continue
            if not str(a.get('downloadURL', '')).startswith('http'):
                continue
            key = f"{a.get('name', '')}|{a.get('version', '')}"
            if key in seen:
                continue
            seen.add(key)
            apps.append(slim(a))
    if len(apps) < 100:
        raise RuntimeError(f'upstream degraded: only {len(apps)} apps')
    apps.sort(key=lambda x: str(x.get('versionDate') or ''), reverse=True)
    return apps, len(docs)


def build_body(source_url):
    apps, n_ok = aggregate()
    doc = {
        'name': '聚合软件源',
        'identifier': 'io.github.aa926453.aggappstore',
        'sourceURL': source_url,
        'message': f'实时聚合 {n_ok}/{len(SOURCES)} 个上游源 · 共 {len(apps)} 个应用',
        'apps': apps,
    }
    return json.dumps(doc, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def serve():
    now = time.time()
    if not _cache['body'] or now - _cache['ts'] > CACHE_TTL:
        _cache['body'] = build_body(SOURCE_URL)
        _cache['ts'] = now
    return _cache['body']


def handler(a, b=None):
    # WSGI 模式（HTTP 触发器）
    if isinstance(a, dict) and callable(b):
        try:
            body = serve()
            status = '200 OK'
        except Exception:
            status, body = '502 Bad Gateway', b'upstream error'
        b(status, [
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Access-Control-Allow-Origin', '*'),
            ('Cache-Control', 'no-store'),
        ])
        return [body]
    # 事件模式兜底
    return serve()
