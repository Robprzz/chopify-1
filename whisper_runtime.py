"""One inference policy for short, chunked, Live and subtitle transcriptions."""
import time

import gpu_runtime


class Session:
    def __init__(self, model, requested, compute, load, report=None):
        if requested not in ("auto", "cuda", "cpu"):
            raise ValueError("Selecciona CPU, NVIDIA CUDA o Automático.")
        self.name, self.requested = model, requested
        self.preferred, self.load = compute, load
        self.report = report or (lambda message: print(message, flush=True))
        self.model = self.device = self.compute = self.fallback = None

    def _use_cpu(self, reason):
        self.model = None
        self.device, self.compute = "cpu", "int8"
        self.fallback = reason
        self.report(reason)

    def run(self, infer, *, cuda_block=None):
        """Only return completed inference, including the full lazy iterable."""
        started = time.monotonic()
        if self.device is None:
            if self.requested == "cpu":
                self.device = "cpu"
                self.compute = self.preferred if self.preferred not in (None, "auto", "default") else "int8"
            elif reason := cuda_block or gpu_runtime.candidate():
                self._use_cpu(f"{reason} Whisper usará CPU (int8).")
            else:
                self.device = "cuda"
                self.compute = self.preferred if self.preferred not in (None, "auto", "default") else "float16"
        try:
            result, seconds = self._infer(infer)
        except (RuntimeError, OSError, ImportError, ValueError) as exc:
            if self.device != "cuda" or not gpu_runtime.cuda_error(exc):
                raise
            phase = "cargar el modelo" if self.model is None else "transcribir el audio"
            self._use_cpu(f"Falló CUDA al {phase}. Se reintentó en CPU (int8).")
            # The exception traceback may retain GPU weights; leave this block before retrying.
            retry = True
        else:
            retry = False
        if retry:
            result, seconds = self._infer(infer)
        backend = getattr(self.model, "model", None)
        actual = getattr(backend, "device", None)
        return result, {"requested": self.requested, "device": str(actual or "sin confirmar"),
                        "model": self.name, "compute_type": str(getattr(backend, "compute_type", None) or self.compute),
                        "inference_seconds": round(seconds, 4), "elapsed_seconds": round(time.monotonic() - started, 4),
                        "fallback": self.fallback}

    def _infer(self, infer):
        if self.model is None:
            self.model = self.load(self.name, self.device, self.compute)
        started = time.monotonic()
        result = infer(self.model)
        return result, time.monotonic() - started
