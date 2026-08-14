#!/usr/bin/env python3
"""Bounded subprocess execution that also terminates descendant processes."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence


class _WindowsJob:
    """Best-effort Windows Job Object with kill-on-close semantics."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BASIC_ACCOUNTING(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(handle)
            return
        self.handle = handle
        self.kernel32 = kernel32
        self.accounting_type = BASIC_ACCOUNTING

    def assign(self, process: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        if not self.kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):
            return False
        return True

    def resume(self, process: subprocess.Popen) -> bool:
        """Resume a process created with CREATE_SUSPENDED after job assignment."""
        if not self.handle:
            return False
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = wintypes.LONG
        return int(ntdll.NtResumeProcess(int(process._handle))) == 0

    def terminate(self) -> None:
        if self.handle:
            self.kernel32.TerminateJobObject(self.handle, 1)

    def active_process_count(self) -> int | None:
        if not self.handle:
            return 0
        import ctypes

        accounting = self.accounting_type()
        if not self.kernel32.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            return None
        return int(accounting.ActiveProcesses)

    def wait_empty(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            active = self.active_process_count()
            if active == 0:
                return True
            if active is None or time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _join_output(partial, final) -> str:
    left = _text(partial)
    right = _text(final)
    if not left:
        return right
    if not right or right in left:
        return left
    if left in right:
        return right
    return left + right


def terminate_process_tree(
    process: subprocess.Popen, *, job: _WindowsJob | None = None, grace_seconds: float = 8.0,
) -> None:
    """Terminate only the process tree rooted at a process launched by this module."""
    owned_job = job or getattr(process, "_bounded_process_job", None)
    if owned_job and owned_job.handle:
        try:
            owned_job.terminate()
            owned_job.wait_empty(grace_seconds)
        finally:
            owned_job.close()
    elif os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(1.0, grace_seconds),
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def spawn_managed(
    command: Sequence[str | os.PathLike],
    *,
    cwd: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    stdin=None,
    stdout=None,
    stderr=None,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    creationflags: int = 0,
) -> subprocess.Popen:
    """Launch a process in an owned process tree before any child code can run.

    On Windows the process starts suspended, is assigned to a kill-on-close Job
    Object, and is only then resumed.  Failure to establish that ownership is a
    hard launch failure, never a silent downgrade to direct-child termination.
    """
    normalized = [str(value) for value in command]
    flags = int(creationflags or 0)
    popen_kwargs = {}
    job = None
    if os.name == "nt":
        job = _WindowsJob()
        if not job.handle:
            raise RuntimeError("windows_job_creation_failed")
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            normalized,
            cwd=str(Path(cwd)) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            encoding=encoding,
            errors=errors,
            creationflags=flags,
            **popen_kwargs,
        )
    except Exception:
        if job:
            job.close()
        raise

    if job:
        try:
            if not job.assign(process):
                raise RuntimeError("windows_job_assignment_failed")
            setattr(process, "_bounded_process_job", job)
            if not job.resume(process):
                raise RuntimeError("windows_suspended_process_resume_failed")
        except BaseException:
            # The process is still suspended until resume succeeds.  Always
            # close/terminate the owned job and then reap the direct process;
            # this also covers an exception raised by a Win32 API wrapper.
            terminate_process_tree(process, job=job, grace_seconds=2)
            raise
    return process


def close_process_tree_job(process: subprocess.Popen) -> None:
    """Release tree ownership after a clean command exit, killing stray children."""
    job = getattr(process, "_bounded_process_job", None)
    if job and job.handle:
        try:
            job.terminate()
            job.wait_empty(2.0)
        finally:
            job.close()
    elif os.name != "nt":
        # The direct process has already exited, but descendants can remain in
        # the new session/process group created by spawn_managed().
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def run_bounded(
    command: Sequence[str | os.PathLike],
    *,
    timeout: float,
    cwd: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    creationflags: int = 0,
) -> subprocess.CompletedProcess:
    """Run a command with UTF-8 capture and a hard process-tree wall clock.

    A normal ``subprocess.run(..., timeout=...)`` can remain blocked on Windows
    when a killed parent leaves browser descendants holding stdout/stderr pipe
    handles.  This helper owns a new process group and kills that exact tree.
    Timeouts are returned as exit code 124 so callers can persist diagnostics.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    normalized = [str(value) for value in command]
    process = spawn_managed(
        normalized,
        cwd=str(Path(cwd)) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        close_process_tree_job(process)
        return subprocess.CompletedProcess(normalized, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process)
        try:
            tail_stdout, tail_stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            tail_stdout, tail_stderr = "", ""
        marker = f"subprocess_timeout: exceeded {float(timeout):.1f}s wall-clock timeout"
        stderr = _join_output(exc.stderr, tail_stderr)
        stderr = "\n".join(value for value in (stderr, marker) if value)
        stdout = _join_output(exc.stdout, tail_stdout)
        return subprocess.CompletedProcess(normalized, 124, stdout, stderr)
    except BaseException:
        terminate_process_tree(process)
        raise


def run_persistent_launcher(
    command: Sequence[str | os.PathLike],
    *,
    timeout: float,
    cwd: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a short launcher while preserving its intentionally detached child.

    This is only for a launcher whose persistent child explicitly redirects
    stdin/stdout/stderr away from the launcher's pipes.  On success the child
    remains alive; on timeout or any exception the exact launcher tree is
    terminated.  Browser capture workers must continue to use ``run_bounded``.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    normalized = [str(value) for value in command]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    process = subprocess.Popen(
        normalized,
        cwd=str(Path(cwd)) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(normalized, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process, grace_seconds=5)
        try:
            tail_stdout, tail_stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            tail_stdout, tail_stderr = "", ""
        marker = f"persistent_launcher_timeout: exceeded {float(timeout):.1f}s wall-clock timeout"
        stderr = "\n".join(value for value in (
            _join_output(exc.stderr, tail_stderr), marker,
        ) if value)
        return subprocess.CompletedProcess(
            normalized, 124, _join_output(exc.stdout, tail_stdout), stderr,
        )
    except BaseException:
        terminate_process_tree(process, grace_seconds=5)
        raise
