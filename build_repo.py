from pathlib import Path
import subprocess, hashlib, gzip, bz2
from datetime import datetime, timezone
root=Path(__file__).resolve().parent
entries=[]
architectures=set()
for p in sorted((root/'debs').rglob('*.deb')):
    control=subprocess.check_output(['dpkg-deb','-f',str(p)],text=True).strip()
    fields=dict(line.split(':',1) for line in control.splitlines() if line and not line.startswith((' ','\t')) and ':' in line)
    for key in ('Package','Version','Architecture'):
        if not fields.get(key,'').strip(): raise ValueError(f'{p}: missing {key}')
    architectures.add(fields['Architecture'].strip())
    data=p.read_bytes()
    control='\n'.join(line for line in control.splitlines() if not line.startswith(('Filename:','Size:','MD5sum:','SHA1:','SHA256:')))
    entries.append(control+'\nFilename: '+p.relative_to(root).as_posix()+'\nSize: '+str(len(data))+'\nMD5sum: '+hashlib.md5(data).hexdigest()+'\nSHA1: '+hashlib.sha1(data).hexdigest()+'\nSHA256: '+hashlib.sha256(data).hexdigest()+'\n\n')
data=''.join(entries).encode()
(root/'Packages').write_bytes(data)
(root/'Packages.gz').write_bytes(gzip.compress(data,mtime=0))
(root/'Packages.bz2').write_bytes(bz2.compress(data))
release='Origin: 不知道起什么名\nLabel: 不知道起什么名\nSuite: stable\nCodename: stable\nVersion: 1.0\nArchitectures: '+ ' '.join(sorted(architectures or {'iphoneos-arm','iphoneos-arm64','iphoneos-arm64e'}))+'\nComponents: main\nDescription: vipios 的 Sileo 软件源\nDate: '+datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')+'\n'
for heading,alg in [('MD5Sum','md5'),('SHA1','sha1'),('SHA256','sha256')]:
    release+=heading+':\n'
    for name in ('Packages','Packages.gz','Packages.bz2'):
        b=(root/name).read_bytes()
        release+=f' {hashlib.new(alg,b).hexdigest()} {len(b)} {name}\n'
(root/'Release').write_text(release)
print('Indexed',len(entries),'packages')
