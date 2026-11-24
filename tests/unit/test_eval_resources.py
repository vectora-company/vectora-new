"""Testes da amostragem de recursos do processo do app."""

from __future__ import annotations

import os
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from backend.evaluation.resources import ProcessTreeResourceSampler


def test_sampler_correlates_process_metrics_without_inventing_gpu_values() -> None:
    sampler = ProcessTreeResourceSampler(
        os.getpid(),
        run_id="resource-run",
        scenario_id="installed-app-startup",
        interval_seconds=0.01,
    )
    sampler.start()
    time.sleep(0.05)
    metrics = sampler.stop()

    assert {metric.name for metric in metrics} == {
        "process_tree_cpu_percent_avg",
        "process_tree_cpu_percent_peak",
        "process_tree_rss_peak",
        "process_tree_members_peak",
        "process_tree_gpu_memory_peak",
    }
    assert all(metric.run_id == "resource-run" for metric in metrics)
    assert all(metric.scenario_id == "installed-app-startup" for metric in metrics)
    assert (
        next(
            metric for metric in metrics if metric.name == "process_tree_rss_peak"
        ).status
        == "measured"
    )
    gpu = next(
        metric for metric in metrics if metric.name == "process_tree_gpu_memory_peak"
    )
    if gpu.status == "unavailable":
        assert gpu.value is None
        assert gpu.reason
    else:
        assert gpu.status == "measured"
        assert gpu.value is not None and gpu.value >= 0
    assert sampler.stop() == metrics


def test_sampler_measures_nvidia_memory_for_matching_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nvml = ModuleType("pynvml")
    nvml.__dict__.update(
        {
            "nvmlInit": lambda: None,
            "nvmlShutdown": lambda: None,
            "nvmlDeviceGetCount": lambda: 1,
            "nvmlDeviceGetHandleByIndex": lambda _index: "gpu-0",
            "nvmlDeviceGetComputeRunningProcesses": lambda _handle: [
                SimpleNamespace(pid=os.getpid(), usedGpuMemory=64 * 1024 * 1024)
            ],
            "nvmlDeviceGetGraphicsRunningProcesses": lambda _handle: [],
        }
    )
    monkeypatch.setitem(sys.modules, "pynvml", nvml)

    sampler = ProcessTreeResourceSampler(
        os.getpid(),
        run_id="nvidia-run",
        scenario_id="installed-app-startup",
    )
    sampler.start()
    gpu = next(
        metric
        for metric in sampler.stop()
        if metric.name == "process_tree_gpu_memory_peak"
    )

    assert gpu.status == "measured"
    assert gpu.value == pytest.approx(64)
    assert gpu.source == "pynvml.process_tree.usedGpuMemory"
