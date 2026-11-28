"""Medições da árvore de processos sem tratar sensores ausentes como zero."""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from types import ModuleType

import psutil

from backend.evaluation.metrics import BenchmarkMetric


class ProcessTreeResourceSampler:
    """Amostra CPU e RSS do app e filhos enquanto o processo está ativo."""

    def __init__(
        self,
        root_pid: int,
        *,
        run_id: str,
        scenario_id: str,
        interval_seconds: float = 0.2,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("intervalo de amostragem precisa ser positivo")
        self.root_pid = root_pid
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._finished = False
        self._metrics: list[BenchmarkMetric] | None = None
        self._previous: tuple[float, float] | None = None
        self._cpu_percentages: list[float] = []
        self._peak_rss_bytes = 0
        self._peak_process_count = 0
        self._nvml: ModuleType | None = None
        self._nvml_device_count = 0
        self._gpu_unavailable_reason = "NVML ainda não foi consultado."
        self._gpu_memory_samples: list[int] = []

    def start(self) -> None:
        """Inicia amostragem em thread daemon para não prender o encerramento."""
        if self._thread is not None:
            raise RuntimeError("amostrador já iniciado")
        self._initialize_nvml()
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name=f"vectora-eval-resources-{self.root_pid}",
            daemon=True,
        )
        self._thread.start()

    def _process_tree(self) -> list[psutil.Process]:
        try:
            root = psutil.Process(self.root_pid)
            return [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return []

    def _sample(self) -> None:
        now = time.monotonic()
        cpu_seconds = 0.0
        rss_bytes = 0
        live_count = 0
        processes = self._process_tree()
        process_ids = {process.pid for process in processes}
        for process in processes:
            try:
                cpu = process.cpu_times()
                memory = process.memory_info()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            cpu_seconds += cpu.user + cpu.system
            rss_bytes += memory.rss
            live_count += 1

        with self._lock:
            self._peak_rss_bytes = max(self._peak_rss_bytes, rss_bytes)
            self._peak_process_count = max(self._peak_process_count, live_count)
            if self._previous is not None:
                previous_time, previous_cpu = self._previous
                elapsed = now - previous_time
                if elapsed > 0:
                    cpu_percent = max(0.0, (cpu_seconds - previous_cpu) / elapsed * 100)
                    self._cpu_percentages.append(cpu_percent)
            self._previous = (now, cpu_seconds)
        self._sample_gpu_memory(process_ids)

    def _initialize_nvml(self) -> None:
        """Open NVML when an NVIDIA driver and process accounting are available."""
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._nvml_device_count = int(pynvml.nvmlDeviceGetCount())
        except Exception as exc:
            self._nvml = None
            self._gpu_unavailable_reason = f"NVML indisponível ({type(exc).__name__})."
            return
        if self._nvml_device_count == 0:
            self._gpu_unavailable_reason = (
                "Nenhuma GPU NVIDIA compatível foi encontrada."
            )
        else:
            self._gpu_unavailable_reason = (
                "NVML não mostrou processos da árvore do app durante a amostragem."
            )

    def _sample_gpu_memory(self, process_ids: set[int]) -> None:
        """Sample process-attributed compute/graphics GPU memory via NVML."""
        if self._nvml is None or self._nvml_device_count == 0 or not process_ids:
            return
        total_bytes = 0
        found_process = False
        incomplete = False
        for index in range(self._nvml_device_count):
            handle = None
            with suppress(Exception):
                handle = self._nvml.nvmlDeviceGetHandleByIndex(index)
            if handle is None:
                continue
            device_memory: dict[int, int] = {}
            for query_name in (
                "nvmlDeviceGetComputeRunningProcesses",
                "nvmlDeviceGetGraphicsRunningProcesses",
            ):
                query = getattr(self._nvml, query_name, None)
                if not callable(query):
                    continue
                running = None
                with suppress(Exception):
                    running = query(handle)
                if running is None:
                    continue
                for entry in running:
                    if entry.pid not in process_ids:
                        continue
                    found_process = True
                    memory_bytes = entry.usedGpuMemory
                    if memory_bytes is None or not isinstance(memory_bytes, int):
                        incomplete = True
                        continue
                    device_memory[entry.pid] = max(
                        device_memory.get(entry.pid, 0), memory_bytes
                    )
            total_bytes += sum(device_memory.values())
        if found_process and not incomplete:
            self._gpu_memory_samples.append(total_bytes)
            self._gpu_unavailable_reason = ""
        elif found_process:
            self._gpu_unavailable_reason = (
                "O driver não disponibilizou memória por processo "
                "(por exemplo, WDDM no Windows)."
            )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def stop(self) -> list[BenchmarkMetric]:
        """Fecha a amostragem e devolve métricas medidas ou indisponíveis."""
        with self._lock:
            if self._finished:
                return list(self._metrics or [])
            self._finished = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self._sample()
        with self._lock:
            cpu_values = list(self._cpu_percentages)
            peak_rss_bytes = self._peak_rss_bytes
            peak_process_count = self._peak_process_count
        if self._nvml is not None:
            with suppress(Exception):
                self._nvml.nvmlShutdown()

        metrics: list[BenchmarkMetric] = []
        if cpu_values:
            metrics.append(
                self._metric(
                    name="process_tree_cpu_percent_avg",
                    value=sum(cpu_values) / len(cpu_values),
                    unit="percent_of_one_core",
                    source="psutil.process_tree.cpu_times",
                )
            )
            metrics.append(
                self._metric(
                    name="process_tree_cpu_percent_peak",
                    value=max(cpu_values),
                    unit="percent_of_one_core",
                    source="psutil.process_tree.cpu_times",
                )
            )
        else:
            metrics.append(
                self._unavailable(
                    "process_tree_cpu_percent_avg",
                    "percent_of_one_core",
                    "A execução terminou antes de haver duas amostras de CPU.",
                )
            )

        if peak_process_count:
            metrics.append(
                self._metric(
                    name="process_tree_rss_peak",
                    value=peak_rss_bytes / (1024 * 1024),
                    unit="MiB",
                    source="psutil.process_tree.memory_info.rss",
                )
            )
            metrics.append(
                self._metric(
                    name="process_tree_members_peak",
                    value=float(peak_process_count),
                    unit="processes",
                    source="psutil.process_tree",
                )
            )
        else:
            metrics.append(
                self._unavailable(
                    "process_tree_rss_peak",
                    "MiB",
                    "O sistema não permitiu observar a árvore de processos.",
                )
            )

        if self._gpu_memory_samples:
            metrics.append(
                self._metric(
                    name="process_tree_gpu_memory_peak",
                    value=max(self._gpu_memory_samples) / (1024 * 1024),
                    unit="MiB",
                    source="pynvml.process_tree.usedGpuMemory",
                )
            )
        else:
            metrics.append(
                self._unavailable(
                    "process_tree_gpu_memory_peak",
                    "MiB",
                    self._gpu_unavailable_reason,
                )
            )
        with self._lock:
            self._metrics = metrics
        return list(metrics)

    def _metric(
        self, *, name: str, value: float, unit: str, source: str
    ) -> BenchmarkMetric:
        return BenchmarkMetric(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            stage="installed_app_process_tree",
            name=name,
            value=value,
            unit=unit,
            dimension="resources",
            source=source,
        )

    def _unavailable(self, name: str, unit: str, reason: str) -> BenchmarkMetric:
        return BenchmarkMetric(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            stage="installed_app_process_tree",
            name=name,
            value=None,
            unit=unit,
            dimension="resources",
            source="evaluation.process_tree_sampler",
            status="unavailable",
            reason=reason,
        )
