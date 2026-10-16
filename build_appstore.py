#!/usr/bin/env python3
"""全能签聚合源构建：实时抓取上游源 → 过滤锁定/无效项 → 去重合并 → 输出 appstore.json

更新统计说明（GitHub Actions 无状态环境）：
- 30 分钟更新数 = 与仓库中上次提交的 appstore.json 对比新增的应用
- 今日更新数 = versionDate 为今天（北京时间）的应用数
"""
import json
import os
import sys
import time
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
SOURCE_NAME = '不知道起什么名'
SOURCE_ICON = 'https://aa926453.github.io/flag2.png'
OWNER_QQ = '1505435445'
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


def load_prev_keys():
    """读仓库中上次提交的 appstore.json，作为 30 分钟对比基线"""
    try:
        with open(OUT, encoding='utf-8') as f:
            old = json.load(f)
        return {f"{a.get('name', '')}|{a.get('version', '')}"
                for a in old.get('apps', [])}
    except Exception:
        return set()


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

    # ---- 更新统计 ----
    prev_keys = load_prev_keys()
    updated_30min = len(seen - prev_keys) if prev_keys else 0
    today = time.strftime('%Y-%m-%d', time.gmtime(time.time() + 8 * 3600))  # 北京时间
    updated_today = sum(1 for a in apps
                        if str(a.get('versionDate', '')).startswith(today))

    apps.sort(key=lambda a: str(a.get('versionDate') or ''), reverse=True)

    message = (f'共 {len(apps)} 个应用\n'
               f'最近 30 分钟更新 {updated_30min} 个 · 今日更新 {updated_today} 个\n'
               f'源主 QQ：{OWNER_QQ}')

    doc = {
        'name': SOURCE_NAME,
        'identifier': 'io.github.aa926453.aggappstore',
        'sourceURL': SOURCE_URL,
        'sourceicon': SOURCE_ICON,
        'message': message,
        'apps': apps,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))

    print(f'  上游: {" ".join(per_source)}')
    print(f'  合计: {len(apps)} 个应用 -> appstore.json '
          f'({os.path.getsize(OUT) / 1048576:.1f}MB)')
    print(f'  统计: 30分钟更新 {updated_30min} | 今日更新 {updated_today}')


if __name__ == '__main__':
    main()
