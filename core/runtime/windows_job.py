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

    def resume(self, process_id: int) -> None:
        """Resume a CREATE_SUSPENDED child only after Job assignment.

        Win32 subprocess.Popen closes its initial thread handle. Toolhelp
        enumerates the child's suspended thread, avoiding an undocumented NT
        API and the Popen-to-Assign race that can leak a fast grandchild.
        """
        from ctypes import wintypes

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        dll = self._dll
        dll.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        dll.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
        dll.Thread32First.restype = wintypes.BOOL
        dll.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
        dll.Thread32Next.restype = wintypes.BOOL
        dll.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        dll.OpenThread.restype = wintypes.HANDLE
        dll.ResumeThread.argtypes = (wintypes.HANDLE,)
        dll.ResumeThread.restype = wintypes.DWORD

        snapshot = dll.CreateToolhelp32Snapshot(0x00000004, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == invalid_handle:
            raise OSError(ctypes.get_last_error(), "Thread snapshot failed")
        resumed = 0
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            active = dll.Thread32First(snapshot, ctypes.byref(entry))
            while active:
                if int(entry.th32OwnerProcessID) == process_id:
                    thread = dll.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise OSError(ctypes.get_last_error(), "OpenThread failed")
                    try:
                        prior_count = dll.ResumeThread(thread)
                        if prior_count == 0xFFFFFFFF:
                            raise OSError(ctypes.get_last_error(), "ResumeThread failed")
                        resumed += 1
                    finally:
                        dll.CloseHandle(thread)
                active = dll.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            dll.CloseHandle(snapshot)
        if not resumed:
            raise RuntimeError("No suspended process thread found")

    def terminate(self) -> None:
        if self._handle:
            if not self._dll.TerminateJobObject(self._handle, 1):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._dll.CloseHandle(handle)
