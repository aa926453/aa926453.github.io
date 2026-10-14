#!/usr/bin/env python3
"""Index aggregation only: preserves original package fields and authorization."""
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
import bz2, gzip, hashlib, re, subprocess, sys
ROOT=Path(__file__).resolve().parent
BASE='https://wkkyy00.github.io/'

def records(data):
    out=[]
    for block in re.split(r'\n\s*\n',data.decode('utf-8').replace('\r\n','\n').strip()):
        if not block.strip():continue
        fields={}
        for line in block.splitlines():
            if line and not line.startswith((' ','\t')) and ':' in line:
                k,v=line.split(':',1);fields[k]=v.strip()
        if any(not fields.get(k) for k in ('Package','Version','Architecture','Filename')):
            raise ValueError('Invalid package stanza')
        out.append((block,fields))
    return out

# Fetch and validate before modifying local index. Failure stops deployment.
with urlopen(Request(BASE+'Packages.bz2',headers={'User-Agent':'SileoIndexSync/1.0'}),timeout=60) as response:
    raw=response.read(8*1024*1024+1)
if len(raw)>8*1024*1024:raise ValueError('Index too large')
remote=records(bz2.decompress(raw))
if not remote:raise ValueError('Empty upstream index')
converted=[]
for block,f in remote:
    url=urljoin(BASE,f['Filename'])
    if urlparse(url).scheme!='https' or urlparse(url).hostname!='wkkyy00.github.io':
        raise ValueError('Unexpected upstream download host')
    lines=[line for line in block.splitlines() if not line.startswith(('Filename:','X-Upstream-Source:'))]
    lines+=['Filename: '+url,'X-Upstream-Source: '+BASE]
    converted.append(('\n'.join(lines),f))
subprocess.run([sys.executable,str(ROOT/'build_repo.py')],check=True)
local=records((ROOT/'Packages').read_bytes())
# Local package wins for the same identifier/version/architecture.
key=lambda f:(f['Package'],f['Version'],f['Architecture'])
seen={key(f) for _,f in local}
merged=list(local)
for block,f in converted:
    if key(f) not in seen:
        merged.append((block,f));seen.add(key(f))
data=''.join(block+'\n\n' for block,_ in merged).encode('utf-8')
(ROOT/'Packages').write_bytes(data)
(ROOT/'Packages.gz').write_bytes(gzip.compress(data,mtime=0))
(ROOT/'Packages.bz2').write_bytes(bz2.compress(data))
release=(ROOT/'Release').read_text()
release=release.split('MD5Sum:',1)[0]
arches=' '.join(sorted({f['Architecture'] for _,f in merged}))
release=re.sub(r'^Architectures:.*$', 'Architectures: '+arches,release,flags=re.M)
release=re.sub(r'^Date:.*$', 'Date: '+datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000'),release,flags=re.M)
for heading,alg in [('MD5Sum','md5'),('SHA1','sha1'),('SHA256','sha256')]:
    release+=heading+':\n'
    for name in ('Packages','Packages.gz','Packages.bz2'):
        b=(ROOT/name).read_bytes()
        release+=f' {hashlib.new(alg,b).hexdigest()} {len(b)} {name}\n'
(ROOT/'Release').write_text(release)
print('Local:',len(local),'Upstream:',len(remote),'Merged:',len(merged))
