#!/usr/bin/env python3
"""全能签聚合源构建：实时抓取上游源 → 过滤锁定/无效项 → 去重合并 → 输出 appstore.json

更新统计说明（GitHub Actions 无状态环境）：
- 30 分钟更新数 = 与仓库中上次提交的 appstore.json 对比新增的应用
- 今日更新数 = versionDate 为今天（北京时间）的应用数
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'appstore.json')

SOURCES = [
    'https://ipa.hxys2.xyz/appstore/',
    'https://app.dumpapps.com/appstore',
    'https://ipa.312752.cn/appstore',
    'https://qnq.nuosike.cn/appstore',
]

UA = 'OmniSign/7.7'
SOURCE_URL = 'https://aa926453.github.io/appstore.json'
SOURCE_NAME = '不知道起什么名'
WORKER_URL = 'https://iosipa-iosapp.vercel.app'
SOURCE_ICON = 'https://aa926453.github.io/flag2.png'
OWNER_QQ = '1505435445'
KEEP_FIELDS = ('name', 'bundleIdentifier', 'version', 'versionDate', 'iconURL',
               'downloadURL', 'size', 'type')
MAX_STR = 120  # 超长字段截断，防止索引膨胀

# 是否给下载链接套鉴权代理（激活功能暂停时设为 False，用直链减小体积）
USE_PROXY = False

# 方案A：不活跃域名黑名单（30天内零更新），直接过滤这些域名的应用
INACTIVE_HOSTS = {
    'pan.mxwlcm.cn', 'pan.pkq7.cn', 'dumppan.iosr.cn', 'pan.113tech.com',
    'pan.iosi.vip', 'pan.wkios.cn', 'pan.52xv.com', 'pan.wxazf.com',
    'pan.drophere.info', 'pan.ioszc.com', 'gfm.zgios.cn', 'pan.appzy.cn',
    'ipawj.nuosike.cn', 'pan.iosrxwy.com', 'pan.iosdz.cn', 'alist.tooios.com',
    'pan.dzapps.cn', 'wp.iosdzapp.cn', 'pan.xyyh.xyz', 'd41.dwnserver.com',
    'sign.drnrt8.cn', 'multimedia.qfile.qq.com', 'panapi.0210.net:4399',
    'pan.quark.cn', 'vfiles.gtimg.cn', 'jianying.kaluo.xyz', 'dl.iosdzapp.cn',
}

# 方案B：超过此天数未更新的应用直接过滤掉
MAX_APP_AGE_DAYS = 365

# ---- AList 网盘（来自 ios.sha0p.top 导航站 + 威锋论坛帖子）----
# 注：share.bjjii.com 底层天翼云盘直链几分钟过期，无法入源，已排除
ALIST_DRIVES = [
    {'site': 'https://pan.ipaxxs.cn', 'label': 'X先生资源',
     'roots': ['/苹果应用 - Appstore apps', '/巨魔商店 - TrollStore',
               '/插件专区 - Plugin Zone'],
     'depth': 3},
    {'site': 'https://pan.qlam.cc', 'label': '清烟网盘',
     'roots': ['/苹果应用 - Appstore apps', '/巨魔商店 - TrollStore',
               '/插件专区 - Plugin Zone'],
     'depth': 3},
    {'site': 'https://pan.iosku.top', 'label': 'iosku网盘',
     'roots': ['/苹果应用', '/巨魔专用工具', '/可在线安装'],
     'depth': 3},
    {'site': 'https://pan.iosxn.cn', 'label': 'iosxn网盘',
     'roots': ['/ipa资源', '/巨魔专区', '/微信专区'],
     'depth': 3},
    {'site': 'https://pan.iosru.com', 'label': '如烟资源库',
     'roots': ['/苹果应用合集', '/WeChat iPa', '/巨魔专用(已更新)',
               '/微信多开 - 实时更新'],
     'depth': 2},
    {'site': 'https://pan.586226.xyz', 'label': '顷酉网盘',
     'roots': ['/巨魔专区', '/插件库', '/最新iOS应用', '/微信专区',
               '/巨魔注入'],
     'depth': 3},
    {'site': 'https://pan.iosfls.cn', 'label': 'iOS福利社',
     'roots': ['/iPA', '/TrollStore', '/Dopamine'],
     'depth': 2},
    {'site': 'https://pan.yeziku.cn', 'label': '叶子资源库',
     'roots': ['/IPA分享', '/IOS巨魔', '/IOS越狱', '/签名工具',
               '/叶子源相关资源'],
     'depth': 3},
    {'site': 'https://pan.v200dd.top', 'label': '小熊网盘',
     'roots': ['/iphone', '/游客分发下载', '/个人存档', '/工具'],
     'depth': 3},
    {'site': 'http://aoxiang.asia:88', 'label': '翱翔资源',
     'roots': ['/翱翔资源', '/个人云盘'],
     'depth': 3},
    {'site': 'https://alist.jiejingfan.vip', 'label': '捷径范网盘',
     'roots': ['/自签', '/Trollstore', '/越狱工具', '/签名工具', '/IPCC'],
     'depth': 3},
]
# 网盘里这些目录与 iOS 应用无关，跳过
EXCLUDE_DIR_KW = ('会员', '订阅', '安卓', 'Android', '电脑', 'PC', '壁纸',
                  '书源', '音源', '快捷指令', '微信素材', '青年大学习',
                  '证书', '教程', 'ROM', '电视', '素材', 'vip', 'VIP',
                  '点歌', '共享ID', '皮肤', '科学上网', '圈x')
VER_RE = re.compile(r'(\d+(?:\.\d+){1,3})')
ALIST_MAX_FILES = 800  # 单盘收录上限，防止失控


def truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes')


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(req, timeout=15) as r:
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
    return out


def app_age_days(app):
    """计算应用距今天数，无法解析返回 9999"""
    vd = str(app.get('versionDate', ''))[:10]
    if len(vd) == 10 and vd[4] == '-':
        try:
            dt = time.strptime(vd, '%Y-%m-%d')
            return int((time.time() - time.mktime(dt)) / 86400)
        except Exception:
            pass
    return 9999


def is_app_valid(app):
    """方案A+B：过滤不活跃域名 + 超期未更新应用"""
    url = str(app.get('downloadURL', ''))
    if not url.startswith('http'):
        return False
    # 方案A：不活跃域名黑名单
    host = urllib.parse.urlparse(url).netloc
    if host in INACTIVE_HOSTS:
        return False
    # 方案B：超过 MAX_APP_AGE_DAYS 未更新
    if app_age_days(app) > MAX_APP_AGE_DAYS:
        return False
    return True


def load_prev_keys():
    """读仓库中上次提交的 appstore.json，作为 30 分钟对比基线"""
    try:
        with open(OUT, encoding='utf-8') as f:
            old = json.load(f)
        return {f"{a.get('name', '')}|{a.get('version', '')}"
                for a in old.get('apps', [])}
    except Exception:
        return set()


def alist_list(site, path):
    """AList V3 API：列出目录内容（自动翻页，456 限流自动重试）"""
    items, page = [], 1
    while page <= 3:
        body = json.dumps({'path': path, 'page': page, 'per_page': 100}).encode()
        req = urllib.request.Request(
            f'{site}/api/fs/list', data=body, method='POST',
            headers={'Content-Type': 'application/json', 'User-Agent': UA})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read().decode('utf-8'))
                break
            except urllib.error.HTTPError as e:
                if e.code == 456 and attempt < 2:  # 触发限流，等一下再试
                    time.sleep(1.5 * (attempt + 1))
                else:
                    raise
        if d.get('code') != 200:
            raise RuntimeError(d.get('message', 'alist error'))
        content = (d.get('data') or {}).get('content') or []
        items.extend(content)
        total = (d.get('data') or {}).get('total', 0)
        if len(items) >= total or not content:
            break
        page += 1
    return items


def alist_walk(drive):
    """广度优先遍历网盘目录，收集 .ipa/.tipa 文件"""
    site = drive['site'].rstrip('/')
    files = []
    frontier = list(drive['roots'])
    for _ in range(drive.get('depth', 3)):
        if not frontier or len(files) >= ALIST_MAX_FILES:
            break
        next_frontier = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(alist_list, site, p): p for p in frontier}
            for fut, path in futs.items():
                try:
                    items = fut.result()
                except Exception as e:
                    print(f'  [warn] AList {site}{path} 失败: {e}',
                          file=sys.stderr)
                    continue
                for it in items:
                    name = str(it.get('name', ''))
                    if it.get('is_dir'):
                        if any(k in name for k in EXCLUDE_DIR_KW):
                            continue
                        next_frontier.append(f'{path}/{name}')
                    elif name.lower().endswith(('.ipa', '.tipa')):
                        files.append((f'{path}/{name}', it))
        frontier = next_frontier
    return files[:ALIST_MAX_FILES]


def alist_apps(drive):
    """网盘文件 → 全能签应用条目"""
    site = drive['site'].rstrip('/')
    apps = []
    for path, it in alist_walk(drive):
        name = it['name']
        stem = name.rsplit('.', 1)[0]
        m = VER_RE.search(stem)
        apps.append({
            'name': stem[:100],
            'bundleIdentifier': 'pan.' + hashlib.md5(
                stem.encode('utf-8')).hexdigest()[:10],
            'version': m.group(1) if m else '1.0',
            'versionDate': str(it.get('modified') or '')[:19],
            'downloadURL': f"{site}/d{urllib.parse.quote(path)}",
            'size': int(it.get('size') or 0),
            'type': 'ipa',
        })
    return apps


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
            if not is_app_valid(app):
                continue
            key = f"{app.get('name', '')}|{app.get('version', '')}"
            if key in seen:
                continue
            seen.add(key)
            apps.append(slim(app))
            cnt += 1
        per_source.append(f"{d.get('name', '?')}:{cnt}")

    # ---- AList 网盘应用合入 ----
    old_doc = None
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding='utf-8') as f:
                old_doc = json.load(f)
        except Exception:
            pass
    old_apps = (old_doc or {}).get('apps', [])

    with ThreadPoolExecutor(max_workers=len(ALIST_DRIVES)) as ex:
        futs = {ex.submit(alist_apps, drv): drv
                for drv in ALIST_DRIVES}
        for fut, drv in futs.items():
            label, host = drv['label'], drv['site'].split('//')[1]
            try:
                # 单盘 hard timeout 180s，超过立即放弃改用缓存
                pan_apps = fut.result(timeout=180)
            except TimeoutError:
                print(f'  [warn] 网盘 {label} 超过 180s，放弃改用缓存',
                      file=sys.stderr)
                pan_apps = []
            except Exception as e:
                print(f'  [warn] 网盘 {label} 失败: {e}', file=sys.stderr)
                pan_apps = []
            cached = [a for a in old_apps
                      if host in str(a.get('downloadURL', ''))]
            # 抓取量为 0 或比上次骤降一半以上 → 网盘不稳，沿用缓存
            if cached and (not pan_apps or len(pan_apps) < len(cached) * 0.5):
                print(f'  [warn] {label} 本次仅 {len(pan_apps)} 个（上次 '
                      f'{len(cached)}），沿用缓存')
                pan_apps = cached
            cnt = 0
            for app in pan_apps:
                if not is_app_valid(app):
                    continue
                key = f"{app.get('name', '')}|{app.get('version', '')}"
                if key in seen:
                    continue
                seen.add(key)
                apps.append(app)
                cnt += 1
            per_source.append(f'{label}:{cnt}')

    # 上游大面积故障保护：结果异常少时保留旧索引
    if len(apps) < 100 and old_doc and len(old_doc.get('apps', [])) > len(apps):
        print(f'  [warn] 聚合仅 {len(apps)} 个，保留旧索引 '
              f'{len(old_doc["apps"])} 个')
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

    # ---- 下载链接：激活开启时套代理，否则直链（减小体积加快刷新）----
    if USE_PROXY:
        for app in apps:
            dl = app.get('downloadURL', '')
            if isinstance(dl, str) and dl.startswith('http'):
                app['downloadURL'] = f'{WORKER_URL}/dl?url={urllib.parse.quote(dl, safe="")}'

    message = (f'共 {len(apps)} 个应用\n'
               f'最近 30 分钟更新 {updated_30min} 个 · 今日更新 {updated_today} 个\n'
               f'源主 QQ：{OWNER_QQ}')

    news = [
        {
            'title': '源公告',
            'identifier': 'io.github.aa926453.aggappstore.notice',
            'caption': f'勿忘国耻，吾辈自强！\n\n源主 QQ：{OWNER_QQ}\n有问题加 QQ 联系',
            'tintColor': '#DE2910',
            'imageURL': 'https://aa926453.github.io/bg_news.jpg',
            'date': today,
            'notify': False,
        },
    ]

    doc = {
        'name': SOURCE_NAME,
        'identifier': 'io.github.aa926453.aggappstore',
        'sourceURL': SOURCE_URL,
        'sourceicon': SOURCE_ICON,
        'message': message,
        'news': news,
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
