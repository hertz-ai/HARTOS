"""Tiny test worker — no GPU, just echoes requests.

Spawned via the centralized dispatcher:
    python -m integrations.service_tools.gpu_worker \\
        integrations.service_tools._test_echo_worker

The dispatcher picks up `_load` and `_synthesize` by convention.
"""


def _load():
    # Simulate a noisy library printing to stdout during load.
    # This must NOT corrupt the JSON protocol channel. A correctly
    # isolated worker redirects fd 1 and sys.stdout to stderr before
    # calling _load(), so these writes go to the log stream.
    import os, sys
    # Tests set this to simulate a cold start slower than startup_timeout.
    _load_s = float(os.environ.get('HEVOLVE_TEST_ECHO_LOAD_S', '0') or 0)
    if _load_s > 0:
        import time as _t
        _t.sleep(_load_s)
    print('[noise] library init message on sys.stdout')
    sys.stdout.write('[noise] raw sys.stdout.write\n')
    try:
        os.write(1, b'[noise] raw os.write to fd 1\n')
    except OSError:
        pass
    return {'message': 'loaded'}


def _synthesize(state, req: dict) -> dict:
    op = req.get('op', 'echo')
    if op == 'crash':
        # Simulate an uncatchable crash
        import os
        os._exit(137)
    if op == 'raise':
        raise RuntimeError('simulated handler failure')
    if op == 'noisy_echo':
        # Handler also prints garbage before returning — protocol must survive.
        print('[noise] handler stdout print')
        import sys
        sys.stdout.write('[noise] handler raw write\n')
        return {'echo': req, 'noisy': True}
    if op == 'sleep':
        import time as _t
        _t.sleep(float(req.get('sleep_s', 0)))
        return {'slept': req.get('sleep_s', 0)}
    if op == 'args':
        import sys as _s
        return {'argv': _s.argv[1:]}
    return {'echo': req, 'state': state}
