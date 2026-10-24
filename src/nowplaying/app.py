"""Single-command launcher: producer (monitor) + consumer (web server).

Spawns the two existing entrypoints as child processes so a single
`uv run nowplaying` is enough: the monitor keeps its synchronous cover
lookups (MusicBrainz / yt-dlp) off the web server's event loop, and either
half can still be run alone (`nowplaying-monitor` / `nowplaying-web`).
"""

import os
import subprocess
import sys
import time

from loguru import logger

from nowplaying import config

MODULES = ('nowplaying.monitor', 'nowplaying.web')
_POLL_SECONDS = 0.5
_STOP_SECONDS = 5.0


def _start() -> list[subprocess.Popen]:
    env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
    return [
        subprocess.Popen([sys.executable, '-m', module], env=env)
        for module in MODULES
    ]


def _stop(procs: list[subprocess.Popen], grace: float = 0.0) -> None:
    """Wait up to `grace` for children to exit, then terminate/kill the rest."""
    deadline = time.monotonic() + grace
    for proc in procs:
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main() -> None:
    procs = _start()
    logger.info(f'Dashboard: http://localhost:{config.PORT}')
    interrupted = False
    try:
        while all(proc.poll() is None for proc in procs):
            time.sleep(_POLL_SECONDS)
        logger.error('[Error] a process exited; stopping the other')
    except KeyboardInterrupt:
        interrupted = True
    finally:
        # On Ctrl+C both children got the signal too; give them time to close
        # the DB / SSE streams cleanly before forcing them down.
        _stop(procs, grace=_STOP_SECONDS if interrupted else 0.0)
    if not interrupted and any(proc.returncode for proc in procs):
        sys.exit(1)


if __name__ == '__main__':
    main()
