#!/usr/bin/env python3
"""全能签聚合源构建：实时抓取上游源 → 过滤锁定/无效项 → 去重合并 → 输出 appstore.json"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'appstore.json')

SOURCES = [
    'https://ipa.hxys2.xyz/appstore/',
    'https://app.dumpapps.com/appstore',
    'https://ipa.312752.cn/appstore',
    'https://qnq.nuosike.cn/appstore',
    'https://app.iosrxwy.com/appstore',
]

UA = 'OmniSign/7.7'
SOURCE_URL = 'https://aa926453.github.io/appstore.json'
SOURCE_NAME = '聚合软件源'
KEEP_FIELDS = ('name', 'bundleIdentifier', 'version', 'versionDate', 'iconURL',
               'downloadURL', 'size', 'tintColor', 'type')
MAX_STR = 300  # 超长字段截断，防止索引膨胀


def truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes')


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(req, timeout=90) as r:
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


def main():
    ok_sources = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(fetch, u): u for u in SOURCES}
        for fut in futs:
            try:
                ok_sources.append(fut.result())
            except Exception as e:
                print(f'  [warn] 源失败: {e}', file=sys.stderr)

    apps, seen, per_source = [], set(), []
    for d in ok_sources:
        cnt = 0
        for app in d.get('apps', []):
            if truthy(app.get('lock', 0)):
                continue
            if truthy(app.get('isLanZouCloud', 0)):
                continue
            if not str(app.get('downloadURL', '')).startswith('http'):
                continue
            key = f"{app.get('name', '')}|{app.get('version', '')}"
            if key in seen:
                continue
            seen.add(key)
            apps.append(slim(app))
            cnt += 1
        per_source.append(f"{d.get('name', '?')}:{cnt}")

    # 上游大面积故障保护：结果异常少时保留旧索引
    old = None
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding='utf-8') as f:
                old = json.load(f)
        except Exception:
            pass
    if len(apps) < 100 and old and len(old.get('apps', [])) > len(apps):
        print(f'  [warn] 聚合仅 {len(apps)} 个，保留旧索引 {len(old["apps"])} 个')
        for n in per_source:
            print(f'  {n}')
        return

    apps.sort(key=lambda a: str(a.get('versionDate') or ''), reverse=True)

    doc = {
        'name': SOURCE_NAME,
        'identifier': 'io.github.aa926453.aggappstore',
        'sourceURL': SOURCE_URL,
        'message': f'聚合 {len(ok_sources)}/{len(SOURCES)} 个上游源 · 共 {len(apps)} 个应用 · 每30分钟自动同步',
        'apps': apps,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))

    print(f'  上游: {" ".join(per_source)}')
    print(f'  合计: {len(apps)} 个应用 -> appstore.json '
          f'({os.path.getsize(OUT) / 1048576:.1f}MB)')


if __name__ == '__main__':
    main()
