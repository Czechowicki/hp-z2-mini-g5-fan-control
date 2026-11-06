#!/usr/bin/python3 -I
"""Select a qualified client at service start, without issuing SMM commands."""
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('guard', HERE/'guard.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
try:
    g.secure(HERE/'pins.json')
    pins = json.loads((HERE/'pins.json').read_text())
    for n in ('guard.py','start.py'):
        g.secure(HERE/n)
        g.require(g.sha((HERE/n).read_bytes()) == pins[n], 'runtime file changed: '+n)
    client = g.identify(pins)
    print('hp-fan selected '+str(client), flush=True)
    args = ['python3','-I','-B',str(client)]
    if client.name != 'start.py':
        args += ['--interval','3']
    os.execv('/usr/bin/python3',args)
except Exception as e:
    print('hp-fan refused: '+str(e),file=sys.stderr)
    sys.exit(79)
