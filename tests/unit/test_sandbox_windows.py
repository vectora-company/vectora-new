"""Sandbox — módulo ``windows.py``: env scrubbing + Job Object best-effort.

Cobre:
- ``build_soft_env``: descarta segredos, mantém variáveis essenciais e
  mescla o ``extra`` da política.
- ``assign_job_object_kill_on_close``: degrada pra None sem kernel32 e
  associa o processo quando tudo está disponível.
"""

from __future__ import annotations

import ctypes as _real_ctypes

import pytest

from backend.sandbox import windows as win
from backend.vtypes.contracts import JsonObject


class TestBuildSoftEnv:
    def test_descarta_segredos_mas_mantem_essenciais(self, monkeypatch) -> None:
        env = {
            "SYSTEMROOT": "C:\\Windows",
            "PATH": "C:\\bin;C:\\Windows\\System32",
            "TEMP": "C:\\Temp",
            "OPENAI_API_KEY": "sk-secret",
            "VECTORA_TOKEN": "pro-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "VECTORA_SANDBOX_LOCKDOWN": "0",
        }
        monkeypatch.setattr(win.os, "environ", env)

        result = win.build_soft_env()

        assert result["SYSTEMROOT"] == "C:\\Windows"
        assert result["PATH"] == env["PATH"]
        assert result["VECTORA_SANDBOX_LOCKDOWN"] == "0"
        assert "OPENAI_API_KEY" not in result
        assert "VECTORA_TOKEN" not in result
        assert "AWS_SECRET_ACCESS_KEY" not in result

    def test_mantem_todas_as_vars_vectora_sandbox(self, monkeypatch) -> None:
        env = {
            "SYSTEMROOT": "C:\\Windows",
            "VECTORA_SANDBOX_SOFT": "1",
            "VECTORA_SANDBOX_WORKSPACE_DIR": "C:\\ws",
        }
        monkeypatch.setattr(win.os, "environ", env)

        result = win.build_soft_env()
        assert result["VECTORA_SANDBOX_SOFT"] == "1"
        assert result["VECTORA_SANDBOX_WORKSPACE_DIR"] == "C:\\ws"

    def test_merge_extra_sobrescreve(self, monkeypatch) -> None:
        monkeypatch.setattr(win.os, "environ", {})
        result = win.build_soft_env({"VECTORA_SANDBOX_SOFT": "1"})
        assert result["VECTORA_SANDBOX_SOFT"] == "1"


class TestAssignJobObject:
    def test_degrada_pra_none_quando_kernel32_indisponivel(self, monkeypatch) -> None:
        class _FakeCtypes:
            @property
            def windll(self):
                raise AttributeError("windll not available")

        monkeypatch.setattr(win, "ctypes", _FakeCtypes())
        assert win.assign_job_object_kill_on_close(999) is None

    def test_associa_processo_quando_tudo_disponivel(self, monkeypatch) -> None:
        calls: dict[str, bool | tuple[int, ...]] = {}
        closed_handles: list[int] = []

        class _FakeKernel32:
            def CreateJobObjectW(self, _a, _b) -> int:
                calls["create"] = True
                return 111

            def SetInformationJobObject(self, job, cls, _info, size) -> None:
                calls["set"] = (job, cls, size)

            def OpenProcess(self, access, _inherit, pid) -> int:
                calls["open"] = (access, pid)
                return 222

            def AssignProcessToJobObject(self, job, hproc) -> None:
                calls["assign"] = (job, hproc)

            def CloseHandle(self, h) -> None:
                closed_handles.append(h)

        class _FakeWindll:
            kernel32 = _FakeKernel32()

        class _FakeCtypes:
            windll = _FakeWindll()
            c_int64 = _real_ctypes.c_int64
            c_uint32 = _real_ctypes.c_uint32
            c_size_t = _real_ctypes.c_size_t
            Structure = _real_ctypes.Structure
            byref = _real_ctypes.byref
            sizeof = _real_ctypes.sizeof

        monkeypatch.setattr(win, "ctypes", _FakeCtypes())

        handle = win.assign_job_object_kill_on_close(999)

        assert handle == 111
        assert calls["assign"] == (111, 222)
        # handle do processo é fechado após a atribuição (não o do job).
        assert closed_handles == [222]

    def test_close_job_handle_noop_com_none(self) -> None:
        # Nunca levanta com handle None.
        win.close_job_handle(None)
