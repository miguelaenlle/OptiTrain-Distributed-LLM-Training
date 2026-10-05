"""Explicit supplemental bounded batch; never changes the primary cap/status."""
import json, resource, time
from pathlib import Path
from fleetlab.adapter import provenance
from fleetlab.cli import dump, dynamics_key, reuse
from fleetlab.core import call
from fleetlab.engine import run, validate
from fleetlab.scenarios import experiment, trace
source=Path('/workspace/.cache/search-607e08c')
root=Path('/workspace/.cache/search-supplemental')
root.mkdir(exist_ok=False)
manifest=json.loads((source/'search-manifest.json').read_text())
assert provenance()==manifest['provenance']
completed=json.loads((source/'results.json').read_text())
finished={(r['regular'],r['spot'],r['policy']) for r in completed}
cache={}
for path in source.iterdir():
 if not path.is_dir() or not (path/'verification.json').exists(): continue
 cfg=json.loads((path/'manifest.json').read_text()); cfg={k:v for k,v in cfg.items() if k not in ['provenance','format_version','exact_dynamics_reuse']}
 key=dynamics_key(cfg)
 if key is not None: cache.setdefault(key,path)
requests=trace(.4*16*manifest['q'],manifest['seed'],manifest['count'])
started=time.monotonic();runs=physical=reused=0;reason=None
for candidate in manifest['candidates']:
 if (candidate['regular'],candidate['spot'],candidate['policy']) in finished:continue
 item={**candidate,'scenarios':{}}
 for scenario in manifest['suite']:
  if time.monotonic()-started>=1800 or runs>=3200:
   reason='supplemental budget exhausted';break
  assert provenance()==manifest['provenance']
  cfg=experiment(**candidate,scenario=scenario,requests=requests,seed=manifest['seed'],overrides=manifest['overrides'])
  cfg.update(horizon_ns=manifest['shared_horizon_ns'],calibration_q=manifest['q'],synthetic_rate_per_second=.4*16*manifest['q'],optimizer_prune=True)
  validate(cfg);key=dynamics_key(cfg)
  name=f"r{candidate['regular']}-s{candidate['spot']}-{candidate['policy']}-{scenario}"
  if key is not None and key in cache:
   m=reuse(cfg,cache[key],root/name,key);reused+=1
  else:
   m=run(cfg,root/name,retain=False,wall_timeout=1800-(time.monotonic()-started));physical+=1
   if key is not None:cache[key]=root/name
  item['scenarios'][scenario]=m;runs+=1
  if not m['feasible']:
   item['skipped_scenarios']=[s for s in manifest['suite'] if s not in item['scenarios']]
   item['rejection_certificate']={'scenario':scenario,'metrics':m};break
 if reason:break
 completed.append(item)
 dump(root/'results.json',completed)
 dump(root/'progress.json',{'covered_pairs':len(completed),'runs':runs,'seconds':time.monotonic()-started,'current':name})
result=call({'op':'rank','suite':manifest['suite'],'results':completed})
result.update(coverage_complete=len(completed)==len(manifest['candidates']),status='complete' if len(completed)==len(manifest['candidates']) else 'incomplete',supplemental_runs=runs,physical_runs=physical,reused_runs=reused,elapsed_seconds=time.monotonic()-started,stop_reason=reason,peak_child_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,primary_batch=str(source),provenance=manifest['provenance'],note='Supplemental bounded batch after primary 1800-second exhaustion; aggregate runtime exceeds the proposed single-batch target and is reported separately.')
dump(root/'search.json',result)
print(json.dumps({k:v for k,v in result.items() if k not in ['ranking','provenance']},indent=2))
