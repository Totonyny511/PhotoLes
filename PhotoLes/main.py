"""Production launcher for the PhotoLes dashboard and Telegram bot."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("photoles.production")
SHUTDOWN_TIMEOUT_SECONDS = 10


def configured_port() -> int:
    """Return Railway's injected port, with a convenient local default."""
    raw_port = os.environ.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("PORT must be a whole number.") from error
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535.")
    return port


def service_commands(port: int) -> tuple[list[str], list[str]]:
    """Build commands for the production web server and polling worker."""
    python = sys.executable
    dashboard = [
        python,
        "-m",
        "gunicorn",
        "admin_dashboard:create_app()",
        "--bind",
        f"0.0.0.0:{port}",
        "--workers",
        "1",
        "--threads",
        "4",
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
        "--capture-output",
    ]
    bot = [python, "bot.py"]
    return dashboard, bot


def stop_processes(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    """Gracefully stop every child, then force-stop any that do not exit."""
    running = [process for process in processes if process.poll() is None]
    for process in running:
        process.terminate()

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while running and time.monotonic() < deadline:
        running = [process for process in running if process.poll() is None]
        if running:
            time.sleep(0.1)

    for process in running:
        process.kill()
    for process in processes:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            LOGGER.error("Child process %s did not stop", process.pid)


def main() -> int:
    """Run both services and fail the container if either stops unexpectedly."""
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    port = configured_port()
    environment = os.environ.copy()
    environment.setdefault("PYTHONUNBUFFERED", "1")
    processes: list[subprocess.Popen[bytes]] = []
    stopping = False

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal stopping
        LOGGER.info("Received signal %s; stopping PhotoLes", signum)
        stopping = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    try:
        for command in service_commands(port):
            LOGGER.info("Starting %s", " ".join(command[1:3]))
            processes.append(
                subprocess.Popen(command, cwd=BASE_DIR, env=environment)
            )

        while not stopping:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    LOGGER.error(
                        "Child process %s exited unexpectedly with status %s",
                        process.pid,
                        exit_code,
                    )
                    return exit_code if exit_code else 1
            time.sleep(0.5)
        return 0
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
