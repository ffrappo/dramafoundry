#!/usr/bin/env python3
"""Reconcile the hosted team-access repair from its merged main revision.

The existing deployment is a source-built Compose stack, not a Git checkout.
Preserve its environment, gateway, web container and all persistent data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path('/opt/dramafoundry')
AUTH = Path('src/novelvideo/api/auth.py')
BASE_SOURCE_SHA256 = '60b398bc9241ac90fd995eca34fc5afc21ce4ce8162f0521fb0383c94049e3c1'


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    revision = sys.argv[1]
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise RuntimeError('Exact merged revision required')
    release = ROOT / 'releases' / revision
    if Path.cwd().resolve() != release:
        raise RuntimeError('Unexpected release directory')
    source, destination = release / AUTH, ROOT / 'dramafoundry' / AUTH
    wanted = source.read_bytes()
    compile(wanted, str(source), 'exec')
    digest = hashlib.sha256(wanted).hexdigest()
    current = hashlib.sha256(destination.read_bytes()).hexdigest()
    if current not in {BASE_SOURCE_SHA256, digest}:
        raise RuntimeError('Hosted authentication source changed; preserve it')
    if current != digest:
        pending = destination.with_suffix('.release')
        pending.write_bytes(wanted)
        os.replace(pending, destination)
    voice_module = ROOT / 'dramafoundry/src/novelvideo/api/routes/projects.py'
    voice_source = voice_module.read_text()
    if '-> dict[str, Any]' in voice_source and 'from typing import Any' not in voice_source:
        voice_source = voice_source.replace('from pathlib import Path\n', 'from pathlib import Path\nfrom typing import Any\n', 1)
        compile(voice_source, str(voice_module), 'exec')
        pending = voice_module.with_suffix('.release')
        pending.write_text(voice_source)
        os.replace(pending, voice_module)
    compose = ['docker', 'compose', '-f', str(ROOT / 'deploy-compose.yml')]
    run(*compose, 'build', 'api')
    # Import the real application with production configuration before replacement.
    run(*compose, 'run', '--rm', '--no-deps', '--entrypoint', 'python', 'api', '-c',
        'import novelvideo.api; print("Production API import succeeded")')
    run(*compose, 'up', '-d', '--no-deps', 'api')
    for _ in range(60):
        try:
            with urllib.request.urlopen('http://127.0.0.1:33710/api/v1/config', timeout=3) as response:
                config = json.load(response)
            if config.get('ok') is True:
                break
        except (OSError, ValueError):
            time.sleep(2)
    else:
        raise RuntimeError('Hosted API did not become ready after release')
    script = "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/app/src/novelvideo/api/auth.py').read_bytes()).hexdigest())"
    actual = subprocess.check_output(['docker', 'exec', 'dramafoundry-api-1', 'python', '-c', script], text=True).strip()
    if actual != digest:
        raise RuntimeError('Running API authentication source differs from merged main')
    receipt = {'commit': revision, 'auth_sha256': digest, 'api': 'dramafoundry-api-1', 'released_at': int(time.time())}
    (ROOT / 'team-access-release.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
