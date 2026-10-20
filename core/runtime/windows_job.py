"""Windows Job Object ownership for local command subprocess families.

Attach the subprocess handle to a JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE job
immediately after Popen; reject the launch on attachment failure rather than
silently falling back to unmanaged descendants. A Windows interactive host
acceptance test is still needed because assignment can fail in pre-existing
job contexts and Popen -> Assign has a brief process-start race.
"""
from __future__ import annotations

import ctypes
import os


class WindowsProcessJob:
    """Own exactly one process tree, closed on all executor exits."""

    def __init__(self, process_handle: int):
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects require win32")
        from ctypes import wintypes

        class LARGE_INTEGER(ctypes.Structure):
            _fields_ = [("QuadPart", ctypes.c_int64)]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", LARGE_INTEGER),
                ("PerJobUserTimeLimit", LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
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

        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        dll.CreateJobObjectW.restype = wintypes.HANDLE
        dll.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        )
        dll.SetInformationJobObject.restype = wintypes.BOOL
        dll.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        dll.AssignProcessToJobObject.restype = wintypes.BOOL
        dll.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        dll.TerminateJobObject.restype = wintypes.BOOL
        dll.CloseHandle.argtypes = (wintypes.HANDLE,)
        dll.CloseHandle.restype = wintypes.BOOL
        self._dll = dll
        self._handle = dll.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        try:
            limits = EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            if not dll.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            if not dll.AssignProcessToJobObject(self._handle, wintypes.HANDLE(process_handle)):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        if self._handle:
            self._dll.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._dll.CloseHandle(handle)
