"""A small long-task-runner adapter. The JSON spec contains an argv, never shell code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.1 * (attempt + 1))


def fingerprint():
    # Runner integration identity: payload, run/output paths and fixture scale
    # are allowed to vary. Scientific/parse parameters need their own smoke.
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--spec', required=True)
    p.add_argument('--run-dir', required=True)
    p.add_argument('--smoke-pass')
    p.add_argument('--smoke', action='store_true')
    args = p.parse_args()
    root = Path(args.run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.spec).read_text(encoding='utf-8-sig'))
    if not isinstance(spec.get('argv'), list) or not all(isinstance(x, str) for x in spec['argv']):
        raise ValueError('spec.argv must be a list of strings')
    if not args.smoke:
        if not args.smoke_pass:
            raise ValueError('A persistent runner integration smoke pass is required')
        evidence = json.loads(Path(args.smoke_pass).read_text(encoding='utf-8-sig'))
        if evidence.get('status') != 'passed' or evidence.get('command_fingerprint') != fingerprint():
            raise ValueError('Runner smoke is failed or incompatible')
        info_path = root / 'run_info.json'
        info = json.loads(info_path.read_text(encoding='utf-8-sig')) if info_path.exists() else {}
        info.update(smoke_pass=str(Path(args.smoke_pass).resolve()), smoke_id=evidence['smoke_id'])
        if spec.get('scientific_smoke_required'):
            scientific_path = spec.get('scientific_smoke_pass')
            if not scientific_path:
                raise ValueError('A compatible scientific smoke pass is required by this job spec')
            scientific = json.loads(Path(scientific_path).read_text(encoding='utf-8-sig'))
            expected = spec.get('scientific_fingerprint')
            if (scientific.get('status') != 'passed' or not scientific.get('smoke_id') or
                    (expected and scientific.get('command_fingerprint') != expected)):
                raise ValueError('Scientific smoke is failed or incompatible')
            info.update(scientific_smoke_pass=str(Path(scientific_path).resolve()),
                        scientific_smoke_id=scientific['smoke_id'])
        save(info_path, info)
    env = os.environ.copy()
    env.update(spec.get('env', {}))
    env['PYTHONUTF8'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    started = time.monotonic()
    (root / 'checkpoints').mkdir(exist_ok=True)
    save(root / 'checkpoints' / 'invocation.json', {'spec': str(Path(args.spec).resolve()), 'started_at': now()})
    def progress(status):
        stamp = now()
        save(root / 'progress.json', {'stage': spec.get('stage', 'external'), 'status': status,
             'done': 1 if status == 'completed' else 0, 'total': 1, 'percent': 100 if status == 'completed' else 0,
             'current_item': spec.get('label', ''), 'elapsed_sec': round(time.monotonic()-started, 1),
             'eta_sec': None, 'updated_at': stamp})
        (root / 'heartbeat.txt').write_text(stamp, encoding='utf-8')
    progress('running')
    with (root / 'command.log').open('a', encoding='utf-8') as log:
        child = subprocess.Popen(spec['argv'], cwd=spec.get('cwd'), env=env, stdout=log, stderr=subprocess.STDOUT)
        while child.poll() is None:
            progress('running')
            time.sleep(1)
    missing = [p for p in spec.get('required_outputs', []) if not Path(p).exists()]
    status = 'completed' if child.returncode == 0 and not missing else 'failed'
    result = {'status': status, 'exit_code': child.returncode, 'missing_outputs': missing,
              'finished_at': now(), 'command_fingerprint': fingerprint()}
    save(root / 'job_result.json', result)
    progress(status)
    return 0 if status == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
