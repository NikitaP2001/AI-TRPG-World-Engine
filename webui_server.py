from __future__ import annotations

import os
import socket
import subprocess
import time
from urllib.parse import urlparse

import uvicorn


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on", "y"}


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _parse_host_port(url: str) -> tuple[str, int]:
    u = urlparse(str(url or "").strip())
    host = u.hostname or "127.0.0.1"
    port = int(u.port or (443 if (u.scheme or "").lower() == "https" else 80))
    return host, port


def _should_manage_local_sa_runtime() -> bool:
    backend = (os.environ.get("LLM_WORLD_SA_BACKEND") or "openrouter").strip().lower()
    if backend != "local":
        return False

    api_key = (os.environ.get("LLM_WORLD_SA_LOCAL_API_KEY") or "").strip().lower()
    if api_key not in {"local", ""}:
        # Non-local credential likely points to externally managed endpoint.
        return False

    auto_manage = _env_flag("LLM_WORLD_SA_LOCAL_AUTO_MANAGE", default=True)
    if not auto_manage:
        return False

    base_url = (os.environ.get("LLM_WORLD_SA_LOCAL_BASE_URL") or "").strip()
    if not base_url:
        return False
    host, port = _parse_host_port(base_url)
    # Auto-manage only loopback runtime.
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    # Current auto manager is Ollama-specific endpoint.
    return int(port) == 11434


def _start_local_sa_runtime_if_needed() -> subprocess.Popen | None:
    if not _should_manage_local_sa_runtime():
        return None

    base_url = (os.environ.get("LLM_WORLD_SA_LOCAL_BASE_URL") or "").strip()
    host, port = _parse_host_port(base_url)
    if _tcp_open(host, port):
        print(f"[local-sa] runtime already running at {host}:{port}")
        return None

    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "LLM_WORLD_SA_BACKEND=local is enabled, but 'ollama' is not installed. "
            "Install Ollama or disable local auto-manage (LLM_WORLD_SA_LOCAL_AUTO_MANAGE=0)."
        )

    # Wait briefly for API socket readiness.
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Failed to start local SA runtime: 'ollama serve' exited early.")
        if _tcp_open(host, port):
            print(f"[local-sa] started runtime at {host}:{port}")
            return proc
        time.sleep(0.25)

    try:
        proc.terminate()
    except Exception:
        pass
    raise RuntimeError("Timed out waiting for local SA runtime to start.")


def _stop_local_sa_runtime(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return

    # Best-effort unload of the SA model before shutting down ollama service.
    model = (os.environ.get("LLM_WORLD_SA_LOCAL_MODEL") or "").strip()
    if model:
        try:
            subprocess.run(["ollama", "stop", model], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=6)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _check_port(host: str, port: int) -> None:
    """Warn if port is already in use by another process."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            # Port is open — check who owns it
            import subprocess as _sp
            try:
                out = _sp.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in out.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.strip().split()
                        pid = parts[-1] if parts else "?"
                        print(
                            f"WARNING: Port {port} is already in use "
                            f"(PID {pid}). Try a different port:\n"
                            f"  $env:LLM_WORLD_WEBUI_PORT={port + 1}; "
                            f"python .\\webui_server.py"
                        )
                        return
            except Exception:
                print(f"WARNING: Port {port} is already in use.")
    except Exception:
        pass  # Port is free


def main() -> None:
    port = int(os.environ.get("LLM_WORLD_WEBUI_PORT", "8000"))
    _check_port("127.0.0.1", port)
    # reload=False for cleaner Ctrl+C handling on Windows
    # Use LLM_WORLD_WEBUI_RELOAD=1 env var if you need hot reload during development
    reload = os.environ.get("LLM_WORLD_WEBUI_RELOAD", "").lower() in ("1", "true", "yes")
    host = os.environ.get("LLM_WORLD_WEBUI_HOST", "0.0.0.0")
    local_runtime_proc = _start_local_sa_runtime_if_needed()
    try:
        uvicorn.run("webui.app:app", host=host, port=port, reload=reload)
    finally:
        _stop_local_sa_runtime(local_runtime_proc)


if __name__ == "__main__":
    main()
