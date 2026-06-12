"""Job management for create_subprocess_shell executions.

Every shell command spawned through the tool system is tracked as a "job":
  - Concurrency limited by a semaphore (configurable via server.conf [jobs]).
  - State persisted to  state/<pid>.json  while the job runs; deleted on completion.
  - Conversation snapshot copied to  state/<pid>_latest_chat.json  at job start
    (kept after completion for post-job LLM context).
  - Background monitor sends a WeChat status report every N minutes.

Configuration (server.conf, section [jobs]):
    max_concurrent          = 3    # max parallel jobs
    check_interval_seconds  = 180  # status-report interval
"""

import asyncio
import configparser
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT_DIR = Path(__file__).resolve().parent
_STATE_DIR = _ROOT_DIR / "state"
_HISTORY_DIR = _ROOT_DIR / "history"
_LATEST_CHAT_FILE = _HISTORY_DIR / "latest_chat.json"
_SERVER_CONF_FILE = _ROOT_DIR / "server.conf"

_DEFAULT_MAX_CONCURRENT = 3
_DEFAULT_CHECK_INTERVAL = 180  # seconds (3 minutes)


def _load_jobs_config() -> tuple[int, int]:
    conf = configparser.ConfigParser()
    if _SERVER_CONF_FILE.exists():
        try:
            conf.read(_SERVER_CONF_FILE, encoding="utf-8")
        except (configparser.Error, OSError):
            pass
    max_concurrent = conf.getint(
        "jobs", "max_concurrent", fallback=_DEFAULT_MAX_CONCURRENT
    )
    check_interval = conf.getint(
        "jobs", "check_interval_seconds", fallback=_DEFAULT_CHECK_INTERVAL
    )
    return max(1, max_concurrent), max(10, check_interval)


class JobManager:
    """Tracks subprocess jobs, enforces concurrency, and reports status via WeChat."""

    def __init__(self) -> None:
        max_concurrent, check_interval = _load_jobs_config()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._check_interval = check_interval
        self._bot: Any = None
        # pid -> (user_id, msg_object)
        self._active: dict[int, tuple[str, Any]] = {}
        self._monitor_task: asyncio.Task | None = None
        _STATE_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Configuration

    def set_bot(self, bot: Any) -> None:
        """Inject the WeChatBot instance used for status notifications."""
        self._bot = bot

    def start_monitor(self) -> None:
        """Launch the background status-reporting loop (call once from async context)."""
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    # ------------------------------------------------------------------
    # Monitor loop

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            await self._report_status()

    async def _report_status(self) -> None:
        if self._bot is None or not self._active:
            return

        jobs = self._read_state_files()
        if jobs:
            lines = [f"当前有 {len(jobs)} 个任务正在执行："]
            for j in jobs:
                lines.append(
                    f"  • PID {j['pid']}  [{j['name']}]  启动于 {j['start_time']}"
                )
        else:
            lines = ["当前没有任务在执行。"]
        text = "\n".join(lines)

        notified: set[str] = set()
        for _pid, (user_id, msg) in list(self._active.items()):
            if user_id and msg is not None and user_id not in notified:
                try:
                    await self._bot.reply(msg, text)
                    notified.add(user_id)
                except Exception:
                    pass

    def _read_state_files(self) -> list[dict]:
        result: list[dict] = []
        for f in sorted(_STATE_DIR.glob("*.json")):
            if f.stem.isdigit():
                try:
                    result.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    pass
        return result

    # ------------------------------------------------------------------
    # Job execution

    async def run_job(
        self,
        command: str,
        name: str,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        user_id: str | None = None,
        msg: Any = None,
        check_returncode: bool = False,
    ) -> str:
        """Acquire a concurrency slot, run *command* as a tracked job, return output."""
        merged_env = {**os.environ, **(env or {})}

        await self._semaphore.acquire()

        # Launch the subprocess
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            self._semaphore.release()
            return f"Error executing command: {exc}"

        pid = proc.pid

        # Persist job state file
        state: dict = {
            "pid": pid,
            "name": name,
            "command": command,
            "start_time": datetime.now().isoformat(timespec="seconds"),
        }
        state_file = _STATE_DIR / f"{pid}.json"
        try:
            state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

        # Snapshot the current conversation history for this job
        chat_snapshot = _STATE_DIR / f"{pid}_latest_chat.json"
        try:
            if _LATEST_CHAT_FILE.exists():
                shutil.copy2(_LATEST_CHAT_FILE, chat_snapshot)
        except OSError:
            pass

        self._active[pid] = (user_id or "", msg)

        try:
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(timeout) if timeout else None,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return "Error: command timed out"
            output = stdout.decode(errors="replace")
            if check_returncode and proc.returncode != 0:
                return f"Error: process exited with code {proc.returncode}:\n{output}"
            return output
        finally:
            # Remove state file and chat snapshot on job completion
            try:
                state_file.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                chat_snapshot.unlink(missing_ok=True)
            except OSError:
                pass
            self._active.pop(pid, None)
            self._semaphore.release()


# ---------------------------------------------------------------------------
# Module-level singleton

_manager: JobManager | None = None


def get_manager() -> JobManager:
    """Return the global JobManager instance (created lazily)."""
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
