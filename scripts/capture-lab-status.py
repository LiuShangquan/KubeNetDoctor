#!/usr/bin/env python3
"""只读采集专用 VM 状态，不导出 kubeconfig/Secret；RSS 是时点值而非压力峰值。"""
import datetime
import importlib.util
import json
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('k', ROOT/'tests/kubernetes/run.py')
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)
report = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'nodes': []}
for n in (1, 2):
    state = k.status(n)
    processes = json.loads(k.ssh(n, 'python3 -c '+shlex.quote('''
import json
from pathlib import Path
result=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try:
        if (p/'comm').read_text().strip() != 'knd-agent': continue
        fields=dict(line.split(':',1) for line in (p/'status').read_text().splitlines() if ':' in line)
        result.append({'pid':int(p.name),'RSS':fields.get('VmRSS','').strip(),'threads':fields.get('Threads','').strip()})
    except (FileNotFoundError,ProcessLookupError): pass
print(json.dumps(result))
''')).stdout)
    assert state['localReady'] and state['fastCapable'] and state['apiReachable'] and not state['lastError'], state
    assert state['desiredGeneration'] == state['appliedGeneration'], state
    assert len(processes) == 1, processes
    report['nodes'].append({'node': n, 'kernel': k.ssh(n, 'uname -r').stdout.strip(),
        'agent': state, 'processes': processes,
        'bpffs': k.ssh(n, 'stat -f -c %T /sys/fs/bpf').stdout.strip(),
        'routes': json.loads(k.ssh(n, 'ip -j route show proto 190').stdout),
        'maps': json.loads(k.ssh(n, 'bpftool -j map show').stdout),
        'programs': json.loads(k.ssh(n, 'bpftool -j prog show').stdout)})
pods = json.loads(k.kubectl('get', 'pods', '-A', '-o', 'json').stdout)['items']
report['pods'] = [{'namespace': p['metadata']['namespace'], 'name': p['metadata']['name'],
    'node': p['spec'].get('nodeName'), 'phase': p['status'].get('phase'), 'IP': p['status'].get('podIP'),
    'containers': [{'name': c['name'], 'ready': c['ready'], 'imageID': c.get('imageID'), 'restarts': c['restartCount']}
                   for c in p['status'].get('containerStatuses', [])]} for p in pods]
report['daemonset'] = json.loads(k.kubectl('-n', 'knd-system', 'get', 'daemonset', 'knd-agent', '-o', 'json').stdout)['status']
assert report['daemonset']['numberReady'] == report['daemonset']['desiredNumberScheduled'] == 2
assert all(p['phase']=='Succeeded' or (p['phase']=='Running' and all(c['ready'] for c in p['containers'])) for p in report['pods']), report['pods']
report['status'] = 'passed'
path = ROOT/'build/final-lab-status.json'
path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': 'passed', 'mode': [n['agent']['mode'] for n in report['nodes']],
    'AgentRSS': [n['processes'][0]['RSS'] for n in report['nodes']], 'pods': len(report['pods'])}))
