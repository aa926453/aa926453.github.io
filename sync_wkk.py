#!/usr/bin/env python3
"""Metadata-only aggregation; no deb downloads, no authentication bypass."""
from pathlib import Path
from urllib.request import Request, urlopen, HTTPSHandler, HTTPRedirectHandler, build_opener
from urllib.parse import urljoin, urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse, bz2, gzip, hashlib, io, json, lzma, re, subprocess, sys, os
ROOT = Path(__file__).resolve().parent
MAX_WIRE = 20 * 1024 * 1024
MAX_PLAIN = 50 * 1024 * 1024

def safe_url(url, https=False):
    u = urlsplit(url)
    if u.scheme not in (('https',) if https else ('https','http')) or not u.hostname or u.username is not None or u.password is not None:
        raise ValueError('Unsafe URL protocol or embedded credentials')
    return url

class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl, True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch(url):
    safe_url(url, True)
    last = None
    for attempt in range(2):
        try:
            opener = build_opener(SecureRedirect())
            with opener.open(Request(url, headers={'User-Agent':'MetadataAggregator/2.0','Accept-Encoding':'identity'}), timeout=12) as r:
                safe_url(r.geturl(), True)
                data = r.read(MAX_WIRE+1)
                if len(data)>MAX_WIRE: raise ValueError('Compressed resource exceeds 20MB')
                return data, r.geturl()
        except Exception as e:
            last = e
    raise ValueError(str(last))

def unpack(raw, name):
    if name.endswith('.gz'): stream = gzip.GzipFile(fileobj=io.BytesIO(raw))
    elif name.endswith('.bz2'): stream = bz2.BZ2File(io.BytesIO(raw))
    elif name.endswith('.xz'): stream = lzma.LZMAFile(io.BytesIO(raw))
    else:
        if len(raw)>MAX_PLAIN: raise ValueError('Plain index exceeds 50MB')
        return raw
    with stream:
        data = stream.read(MAX_PLAIN+1)
    if len(data)>MAX_PLAIN: raise ValueError('Decompressed index exceeds 50MB')
    return data

def records(data):
    text = data.decode('utf-8').replace('\r\n','\n')
    if '<html' in text[:1000].lower() or '<!doctype' in text[:1000].lower(): raise ValueError('HTML is not an APT index')
    out=[]
    for block in re.split(r'\n[ \t]*\n', text.strip()):
        if not block.strip(): continue
        fields={}; invalid=False
        for line in block.splitlines():
            if line and not line.startswith((' ','\t')):
                if ':' not in line: invalid=True; continue
                k,v=line.split(':',1)
                if not re.fullmatch(r'[A-Za-z0-9][-A-Za-z0-9]*',k): invalid=True; continue
                if k in fields and k in ('Package','Version','Architecture','Filename','SHA256','MD5sum'):
                    invalid=True
                fields[k]=v.strip()
        if invalid or any(not fields.get(k) for k in ('Package','Version','Architecture')): continue
        out.append((block,fields))
    if not out: raise ValueError('Empty package index')
    return out

def release_fields(raw):
    text=raw.decode('utf-8')
    if '<html' in text[:1000].lower(): raise ValueError('HTML is not Release')
    fields={}; hashes={}; section=None
    for line in text.splitlines():
        if line.startswith((' ','\t')):
            parts=line.split()
            if section in ('SHA256','SHA1','MD5Sum') and len(parts)==3:
                hashes.setdefault(parts[2],{})[section]=(parts[0],int(parts[1]))
        elif ':' in line:
            k,v=line.split(':',1); fields[k]=v.strip(); section=k
    if not any(k in fields for k in ('Origin','Label','Suite','Codename','Architectures')): raise ValueError('Invalid Release metadata')
    return fields,hashes

def verify(raw,path,hashes):
    h=hashes.get(path,{})
    for kind,alg in [('SHA256','sha256'),('SHA1','sha1'),('MD5Sum','md5')]:
        if kind in h:
            digest,size=h[kind]
            if len(raw)!=size or hashlib.new(alg,raw).hexdigest().lower()!=digest.lower(): raise ValueError('Release hash/size mismatch: '+path)
            return kind
    return 'not-listed'

def normalize(block, f, base):
    if not f.get('Filename'): return None,'missing Filename (metadata/auth-only)'
    target=urljoin(base,f['Filename']); safe_url(target)
    if urlsplit(target).scheme!='https': return None,'insecure HTTP package URL'
    lines=block.splitlines(); result=[]; removing=False
    for line in lines:
        if not line.startswith((' ','\t')):
            removing=line.startswith(('Filename:','X-Upstream-Source:','X-Upstream-Filename:'))
        if not removing: result.append(line)
    result += ['Filename: '+target,'X-Upstream-Source: '+base,'X-Upstream-Filename: '+f['Filename']]
    return '\n'.join(result),None

def worker(source):
    original=source['url']; u=urlsplit(original)
    base=urlunsplit(('https',u.netloc,u.path.rstrip('/')+'/',u.query,''))
    report={'url':original,'effective_url':base,'enabled':source.get('enabled',True),'status':'failed','packages':0,'usable_packages':0,'architectures':[],'errors':[],'indexes':[],'signature_verification':'not performed','reason':source.get('reason','')}
    if source.get('homepage'):
        report['status']='excluded-homepage'; return report,[]
    cache=ROOT/'.sync-cache'/ (hashlib.sha256(base.encode()).hexdigest()+'.json.gz')
    cached=[]
    if cache.exists():
        try:
            saved=json.loads(unpack(cache.read_bytes(),'cache.gz'))
            if saved['base']==base: cached=saved['blocks']
        except Exception as e: report['errors'].append('Cache: '+str(e))
    try:
        suite=source.get('suite'); components=source.get('components',['main'])
        relroot=urljoin(base,'dists/'+suite+'/') if suite else base
        hashes={}; rf={}
        try:
            raw,final=fetch(urljoin(relroot,'Release')); rf,hashes=release_fields(raw)
            report['release_url']=final; report['release_architectures']=rf.get('Architectures','').split()
        except Exception as e:
            report['errors'].append('Release: '+str(e))
            if suite: raise ValueError('dists requires readable Release')
        paths=['Packages']
        if suite:
            arches=rf.get('Architectures','').split() or source.get('architectures',[])
            if not arches: raise ValueError('No architectures in Release')
            paths=[c+'/binary-'+a+'/Packages' for c in components for a in arches]
        all_records=[]; blocks=[]; skipped={}
        for stem in paths:
            got=False
            for ext in ('.bz2','.gz','.xz',''):
                path=stem+ext
                # A dists Release with an index table defines the available variants.
                if suite and hashes and path not in hashes: continue
                try:
                    raw,final=fetch(urljoin(relroot,path)); checked=verify(raw,path,hashes)
                    plain=unpack(raw,path)
                    parsed=records(plain)
                    stanza_count=sum(bool(b.strip()) for b in re.split(r'\n[ \t]*\n',plain.decode('utf-8').replace('\r\n','\n').strip()))
                    damaged=stanza_count-len(parsed)
                    if damaged: skipped['invalid upstream stanza (not rewritten)']=skipped.get('invalid upstream stanza (not rewritten)',0)+damaged
                    if checked=='not-listed' and stem in hashes:
                        checked=verify(unpack(raw,path),stem,hashes)+' (plain)'
                    all_records.extend(parsed)
                    report['indexes'].append({'url':final,'packages':len(parsed),'hash_check':checked,'bytes':len(raw)})
                    for block,f in parsed:
                        try: normalized,error=normalize(block,f,base)
                        except Exception as e: normalized,error=None,str(e)
                        if normalized: blocks.append(normalized)
                        else: skipped[error]=skipped.get(error,0)+1
                    got=True; break
                except Exception as e: report['errors'].append(path+': '+str(e))
            if not got: raise ValueError('No valid index for '+stem)
        report.update(status='success',packages=len(all_records),usable_packages=len(blocks),architectures=sorted({f['Architecture'] for _,f in all_records}),skipped_records=skipped)
        if not blocks: report['status']='metadata-only'
        if blocks and source.get('enabled',True):
            cache.parent.mkdir(exist_ok=True)
            payload=json.dumps({'base':base,'fetched_at':datetime.now(timezone.utc).isoformat(),'blocks':blocks},ensure_ascii=False).encode()
            if len(payload)>MAX_PLAIN: raise ValueError('Cache exceeds 50MB')
            temp=cache.with_suffix('.tmp'); temp.write_bytes(gzip.compress(payload,mtime=0)); temp.replace(cache)
        return report,blocks
    except Exception as e:
        report['errors'].append(str(e))
        if cached and source.get('enabled',True):
            # Revalidate old cache; no untrusted URL is silently replayed.
            good=[]
            for block in cached:
                f=records((block+'\n\n').encode())[0][1]
                safe_url(f['Filename'],True); good.append(block)
            report.update(status='cached',usable_packages=len(good),architectures=sorted({records((b+'\n\n').encode())[0][1]['Architecture'] for b in good}))
            return report,good
        if u.scheme=='http': report['reason']='HTTPS upgrade unavailable; insecure HTTP source disabled'
        return report,[]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--audit-only',action='store_true'); ap.add_argument('--report',default='sync-report.json'); args=ap.parse_args()
    sources=json.loads((ROOT/'sources.json').read_text())['sources']
    with ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(worker,sources))
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'sources':[r for r,_ in results],'conflicts':[],'signature_notice':'Upstream Release signatures are NOT verified. Hash checks only establish consistency, not author authenticity. Aggregated Release requires a new owner signature.'}
    report['summary']={'candidates':len(sources),'success':sum(r['status']=='success' for r,_ in results),'cached':sum(r['status']=='cached' for r,_ in results),'metadata_only':sum(r['status']=='metadata-only' for r,_ in results),'excluded_homepage':sum(r['status']=='excluded-homepage' for r,_ in results),'failed':sum(r['status']=='failed' for r,_ in results),'default_disabled':sum(not r['enabled'] for r,_ in results)}
    def save(): (ROOT/args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save()
    if args.audit_only: print(json.dumps(report['summary'],ensure_ascii=False)); return
    eligible=[(r,b) for r,b in results if r['enabled'] and r['status'] in ('success','cached') and b]
    if not eligible: raise RuntimeError('No successful or cached enabled source; existing build untouched')
    subprocess.run([sys.executable,str(ROOT/'build_repo.py')],check=True)
    local_data=(ROOT/'Packages').read_bytes(); local=records(local_data) if local_data.strip() else []
    merged=[]; seen={}; origins={}
    key=lambda f:(f['Package'],f['Version'],f['Architecture'])
    for block,f in local:
        k=key(f); seen[k]=f; origins[k]='local'; merged.append((block,f))
    duplicates=0
    for r,blocks in eligible:
        for block in blocks:
            f=records((block+'\n\n').encode())[0][1]; k=key(f)
            if k in seen:
                duplicates+=1; old=seen[k]
                mismatched={h:[old.get(h),f.get(h)] for h in ('SHA256','SHA512','SHA1','MD5sum','Size') if old.get(h) and f.get(h) and old[h]!=f[h]}
                if mismatched: report['conflicts'].append({'key':list(k),'kept_source':origins[k],'rejected_source':r['effective_url'],'differences':mismatched})
                continue
            seen[k]=f; origins[k]=r['effective_url']; merged.append((block,f))
    # Compact index: newest Debian version per package and iPhone architecture.
    latest={}
    for block,f in merged:
        if f['Architecture'] not in ('all','iphoneos-arm','iphoneos-arm64','iphoneos-arm64e'): continue
        k=(f['Package'],f['Architecture'])
        if k not in latest or subprocess.run(['dpkg','--compare-versions',f['Version'],'gt',latest[k][1]['Version']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
            latest[k]=(block,f)
    report['summary']['before_compaction']=len(merged)
    merged=list(latest.values())
    data=''.join(b+'\n\n' for b,f in merged).encode()
    files={'Packages':data,'Packages.gz':gzip.compress(data,mtime=0),'Packages.bz2':bz2.compress(data)}
    release=(ROOT/'Release').read_text().split('MD5Sum:',1)[0].split('SHA1:',1)[0].split('SHA256:',1)[0]
    release=re.sub(r'^Architectures:.*$','Architectures: '+' '.join(sorted({f['Architecture'] for _,f in merged})),release,flags=re.M)
    release=re.sub(r'^Date:.*$','Date: '+datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000'),release,flags=re.M)
    for heading,alg in [('MD5Sum','md5'),('SHA1','sha1'),('SHA256','sha256')]:
        release+=heading+':\n'
        for name,b in files.items(): release+=f' {hashlib.new(alg,b).hexdigest()} {len(b)} {name}\n'
    for name,b in files.items(): (ROOT/name).write_bytes(b)
    (ROOT/'Release').write_text(release)
    assert gzip.decompress(files['Packages.gz'])==data and bz2.decompress(files['Packages.bz2'])==data
    _,rh=release_fields(release.encode())
    for name,b in files.items(): assert verify(b,name,rh)=='SHA256'
    report['summary'].update(local=len(local),aggregated_sources=len(eligible),merged=len(merged),duplicates=duplicates,conflicts=len(report['conflicts']),skipped_sources=len(sources)-len(eligible))
    report['output_validation']={'gzip_roundtrip':True,'bz2_roundtrip':True,'Release_SHA256_size':True}
    save(); print(json.dumps(report['summary'],ensure_ascii=False))

if __name__=='__main__': main()
