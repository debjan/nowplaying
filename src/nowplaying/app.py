"""Single-command launcher: producer (monitor) + consumer (web server).

Spawns the two existing entrypoints as child processes so a single
`uv run nowplaying` is enough: the monitor keeps its synchronous cover
lookups (MusicBrainz / yt-dlp) off the web server's event loop, and either
half can still be run alone (`nowplaying-monitor` / `nowplaying-web`).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

from loguru import logger

from nowplaying import config

MODULES = ('nowplaying.monitor', 'nowplaying.web')
_POLL_SECONDS = 0.5
_STOP_SECONDS = 5.0

# --- Windows Job Object plumbing (winbase.h) --------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOBOBJECTINFO_EXTENDED_LIMIT_INFORMATION = 9


class _IoCounters(ctypes.Structure):  # IO_COUNTERS
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount',
    )]


class _BasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitFlags', ctypes.c_ulong),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_ulong),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_ulong),
        ('SchedulingClass', ctypes.c_ulong),
    ]


class _ExtendedLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [
        ('Basic', _BasicLimits),
        ('IoInfo', _IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


_kernel32: ctypes.WinDLL | None = None


def _nt_kernel32() -> ctypes.WinDLL:
    """Cached kernel32 with 64-bit-safe prototypes for the Job Object calls."""
    global _kernel32
    if _kernel32 is None:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32 = kernel32
    return _kernel32


def _job_kill_on_close() -> int | None:
    """Windows: job object that kills its members when the last handle closes.

    Returns None on other platforms or if the API is unavailable (best-effort
    hardening; `_stop()` remains the normal shutdown path either way).
    """
    if os.name != 'nt':
        return None

    kernel32 = _nt_kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    limits = _ExtendedLimits()
    limits.Basic.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel32.SetInformationJobObject(
        job, _JOBOBJECTINFO_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        if job:
            kernel32.CloseHandle(job)
        logger.debug('Job Object unavailable; children not kill-on-close bound')
        return None
    return job


def _start() -> tuple[list[subprocess.Popen], int | None]:
    env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
    job = _job_kill_on_close()
    procs = [
        subprocess.Popen([sys.executable, '-m', module], env=env)
        for module in MODULES
    ]
    if job:
        kernel32 = _nt_kernel32()
        for proc in procs:
            handle = int(getattr(proc, '_handle', 0))  # Windows only; None elsewhere
            if not handle or not kernel32.AssignProcessToJobObject(job, handle):
                logger.debug('AssignProcessToJobObject failed; relying on _stop')
    return procs, job


def _stop(procs: list[subprocess.Popen], grace: float = 0.0) -> None:
    """Wait up to `grace` for children to exit, then terminate/kill the rest."""
    deadline = time.monotonic() + grace
    try:
        for proc in procs:
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass  # second Ctrl+C: skip the grace wait, force the rest down
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
    procs, job = _start()
    interrupted = False
    try:
        logger.info(f'Dashboard: http://localhost:{config.PORT}')
        while all(proc.poll() is None for proc in procs):
            time.sleep(_POLL_SECONDS)
        logger.error('[Error] a process exited; stopping the other')
    except KeyboardInterrupt:
        interrupted = True
    finally:
        # On Ctrl+C both children got the signal too; give them time to close
        # the DB / SSE streams cleanly before forcing them down.
        _stop(procs, grace=_STOP_SECONDS if interrupted else 0.0)
        if job:
            _nt_kernel32().CloseHandle(job)
    if not interrupted and any(proc.returncode for proc in procs):
        sys.exit(1)


if __name__ == '__main__':
    main()
