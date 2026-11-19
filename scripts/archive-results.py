#!/usr/bin/env python3
"""归档已通过的本地实验结果，去掉命令日志；不读取凭证或 VM 私钥。"""
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'docs/experiments'

def read_passed(relative):
    data = json.loads((ROOT/relative).read_text(encoding='utf-8'))
    if data.get('status') != 'passed':
        raise RuntimeError('refuse to archive non-passed result: '+relative)
    data.pop('commands', None)
    return data

def write(name, data):
    (OUT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

for source, name in [
    ('build/daemonset/result.json', 'DaemonSet-results.json'),
    ('build/api-outage/result.json', 'API-outage-results.json'),
    ('build/final-lab-status.json', 'Final-lab-status.json'),
    ('build/m7-candidate-correctness/result.json', 'M7-candidate-results.json')]:
    write(name, read_passed(source))
write('M7-results.json', {name:read_passed(source) for name,source in {
    'comparison':'build/benchmark/comparison.json', 'profile':'build/benchmark/profile-comparison.json',
    'native':'build/benchmark/native-fast/result.json', 'vxlan':'build/benchmark/vxlan-fast/result.json'}.items()})
write('Final-regression-results.json', {
    'archivedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'datapathDefaultELF': read_passed('build/m2/result.json'),
    'buildLog': (ROOT/'build/final-build-test.log').read_text(encoding='utf-8'),
    'note': 'Build log accompanies the successful make invocation recorded during delivery; cached Go tests are identified in the log.'})
manifest = {'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'method':'SHA256 of source, deployment files, documentation, archived evidence and built ELF/binaries; no Git commit exists.', 'files':{}}
paths = set()
for directory in ('cmd','internal','bpf','scripts','tests','deploy','docs'):
    paths.update(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
paths.update(p for p in ROOT.iterdir() if p.is_file())
for directory in ('build/bin','build/bpf'):
    paths.update(p for p in (ROOT/directory).iterdir() if p.is_file() and p.name!='vmlinux.h')
for path in sorted(paths):
    relative = path.relative_to(ROOT).as_posix()
    if relative=='docs/artifact-manifest.json': continue
    manifest['files'][relative] = {'bytes':path.stat().st_size, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
(ROOT/'docs/artifact-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('archived passed results; SHA256 entries:',len(manifest['files']))
