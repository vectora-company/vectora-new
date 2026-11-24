"""Testes da amostragem de recursos do processo do app."""

from __future__ import annotations

import os
import time

from backend.evaluation.resources import ProcessTreeResourceSampler


def test_sampler_correlates_process_metrics_and_marks_gpu_unavailable() -> None:
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
    assert gpu.status == "unavailable"
    assert gpu.value is None
    assert gpu.reason
    assert sampler.stop() == metrics
