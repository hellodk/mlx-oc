#!/usr/bin/env python3
import glob
import json
import urllib.parse
import urllib.request

dash_files = sorted(glob.glob('cluster/mlx_monitoring_bundle/central/dashboards/mlx-monitoring/*.json'))

total_targets = 0
passed_targets = 0
empty_targets = []
syntax_error_targets = []

for fpath in dash_files:
    fname = fpath.split('/')[-1]
    with open(fpath) as f:
        dash = json.load(f)
    print(f"\n========================================================")
    print(f"DASHBOARD: {fname} ({dash.get('title')})")
    print(f"========================================================")
    for p in dash.get('panels', []):
        pid = p['id']
        title = p['title']
        for t in p.get('targets', []):
            total_targets += 1
            raw_expr = t.get('expr', '')
            if not raw_expr:
                continue
            # Substitute Grafana dashboard variables for testing
            q = (
                raw_expr
                .replace('$device', 'mm')
                .replace('$model', 'mlx-community/Qwen2.5-0.5B-Instruct-4bit')
                .replace('$__all_variables', 'var-device=mm&var-model=mlx-community/Qwen2.5-0.5B-Instruct-4bit')
            )
            url = 'http://192.168.1.64:8428/api/v1/query?query=' + urllib.parse.quote(q)
            try:
                req = urllib.request.urlopen(url, timeout=5)
                data = json.loads(req.read().decode())
                res = data.get('data', {}).get('result', [])
                if res:
                    val = res[0].get('value', [None, None])[1]
                    metric_labels = res[0].get('metric', {})
                    passed_targets += 1
                    print(f"  [PASS] P{pid:02d} '{title}' -> len={len(res)} val={val}")
                else:
                    # Empty result is normal for: ALERTS firing, errors, OOM, crashes when healthy
                    print(f"  [EMPTY] P{pid:02d} '{title}' -> len=0 (no active series matching filter)")
                    empty_targets.append((fname, pid, title, raw_expr, q))
            except urllib.error.HTTPError as e:
                err_body = e.read().decode()
                print(f"  [SYNTAX ERROR] P{pid:02d} '{title}' -> HTTP {e.code}: {err_body}")
                syntax_error_targets.append((fname, pid, title, raw_expr, err_body))
            except Exception as e:
                print(f"  [ERROR] P{pid:02d} '{title}' -> {e}")
                syntax_error_targets.append((fname, pid, title, raw_expr, str(e)))

print("\n" + "="*60)
print(f"TOTAL QUERIES VALIDATED: {total_targets}")
print(f"ACTIVE SERIES RETURNED DATA: {passed_targets}")
print(f"EMPTY SERIES (e.g. 0 alerts firing, 0 errors): {len(empty_targets)}")
print(f"SYNTAX / EXECUTION ERRORS: {len(syntax_error_targets)}")
print("="*60)

if syntax_error_targets:
    print("\nSYNTAX ERRORS TO FIX:")
    for fname, pid, title, raw_expr, err in syntax_error_targets:
        print(f"  {fname} P{pid} [{title}]: {err}")
        print(f"    expr: {raw_expr}")
