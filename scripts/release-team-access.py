#!/usr/bin/env python3
"""Reconcile the hosted team-access repair from its merged main revision.

The existing deployment is a source-built Compose stack, not a Git checkout.
Preserve its environment, gateway, web container and all persistent data.
"""
from __future__ import annotations

import ast
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
SOURCES = {
    '93a01e1e7a622cfe1fb7afcfadb2fcb2ab98805dfe4c58fe71d0ff5ed533f85e': 'src/novelvideo/api/auth.py',
    '1431bc8432a434c6411188d5c1d42368c42f0d1401a5f16103ddadd516db2a09': 'src/novelvideo/api/routes/auth.py',
    '75a7f17e0c16677f88eb93ef86eb917662f2c5439e35ce73123ca1ed95d85678': 'src/novelvideo/ports/local/usage.py',
}


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    revision = sys.argv[1]
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise RuntimeError('Exact merged revision required')
    release = ROOT / 'releases' / revision
    if Path.cwd().resolve() != release:
        raise RuntimeError('Unexpected release directory')
    digests = {}
    for base_digest, name in SOURCES.items():
        source, destination = release / name, ROOT / 'dramafoundry' / name
        wanted = source.read_bytes()
        compile(wanted, str(source), 'exec')
        digest = hashlib.sha256(wanted).hexdigest()
        current = hashlib.sha256(destination.read_bytes()).hexdigest()
        if current not in {base_digest, digest}:
            raise RuntimeError(f'Hosted source changed; preserve {name}')
        if current != digest:
            pending = destination.with_suffix('.release')
            pending.write_bytes(wanted)
            os.replace(pending, destination)
        digests[name] = digest
    # Preserve hosted translations outside the repaired producer definitions.
    extraction_path = 'src/novelvideo/structured_extraction.py'
    extraction = ROOT / 'dramafoundry' / extraction_path
    original = extraction.read_text()
    canonical = (release / extraction_path).read_text()
    old_lines, new_lines = original.splitlines(keepends=True), canonical.splitlines(keepends=True)
    old_nodes = {node.name: node for node in ast.parse(original).body if hasattr(node, 'name')}
    new_nodes = {node.name: node for node in ast.parse(canonical).body if hasattr(node, 'name')}
    patches = []
    for name in ['validate_character_chunk_output', '_create_character_extraction_agent', 'extract_characters_from_chunks']:
        wanted = new_nodes[name]
        replacement = ''.join(new_lines[wanted.lineno - 1:wanted.end_lineno])
        current = old_nodes.get(name)
        if current is None:
            if name != 'validate_character_chunk_output':
                raise RuntimeError(f'Hosted extraction definition missing: {name}')
            insertion = old_nodes['_create_character_extraction_agent'].lineno - 1
            patches.append((insertion, insertion, replacement + '\n\n'))
        else:
            patches.append((current.lineno - 1, current.end_lineno, replacement))
    for class_name, field in [('CharacterCandidate', 'evidence'), ('ChunkCharacterOutput', 'characters')]:
        current = next(node for node in old_nodes[class_name].body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == field)
        wanted = next(node for node in new_nodes[class_name].body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == field)
        patches.append((current.lineno - 1, current.end_lineno, ''.join(new_lines[wanted.lineno - 1:wanted.end_lineno])))
    for start, end, replacement in sorted(patches, reverse=True):
        old_lines[start:end] = [replacement]
    extraction_source = ''.join(old_lines)
    compile(extraction_source, str(extraction), 'exec')
    if extraction_source != original:
        pending = extraction.with_suffix('.release')
        pending.write_text(extraction_source)
        os.replace(pending, extraction)
    digests[extraction_path] = hashlib.sha256(extraction.read_bytes()).hexdigest()
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
    run(sys.executable, str(release / 'scripts/reconcile-team-credits.py'), revision)
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
    script = ('import hashlib,json,pathlib; paths=' + repr(list(digests))
              + '; print(json.dumps({p:hashlib.sha256((pathlib.Path("/app")/p).read_bytes()).hexdigest() for p in paths}))')
    actual = json.loads(subprocess.check_output(
        ['docker', 'exec', 'dramafoundry-api-1', 'python', '-c', script], text=True))
    if actual != digests:
        raise RuntimeError('Running API sources differ from merged main')
    receipt = {'commit': revision, 'source_sha256': digests, 'api': 'dramafoundry-api-1', 'released_at': int(time.time())}
    (ROOT / 'team-access-release.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
