"""
后台常驻 Daemon

管理 agent 后台进程的完整生命周期：
- pidfile 单实例锁
- 进程启动/停止/重启
- 日志输出管理
- 健康检查
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# Daemon 状态文件目录
_DAEMON_DIR_NAME = ".mokioclaw"
_PIDFILE_NAME = "daemon.pid"
_LOG_FILE = "daemon.log"


@dataclass
class DaemonInfo:
    """Daemon 进程信息"""
    pid: int = 0
    started_at: str = ""
    workspace: str = ""
    status: str = "unknown"  # running / stopped / stale
    uptime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "workspace": self.workspace,
            "status": self.status,
            "uptime_seconds": self.uptime_seconds,
        }


class DaemonManager:
    """后台 Daemon 管理器

    功能：
    - pidfile 管理（单实例锁）
    - 启动/停止/重启后台 agent
    - 健康检查
    - 日志管理

    使用方式：
        mgr = DaemonManager(workspace=Path("."))
        mgr.start(command=["uv", "run", "dotagent", "serve"])
        info = mgr.get_info()
        mgr.stop()
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or Path.cwd()
        self._daemon_dir = self._workspace / _DAEMON_DIR_NAME
        self._daemon_dir.mkdir(parents=True, exist_ok=True)

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def pidfile(self) -> Path:
        return self._daemon_dir / _PIDFILE_NAME

    @property
    def logfile(self) -> Path:
        return self._daemon_dir / _LOG_FILE

    def is_running(self) -> bool:
        """检查 daemon 是否在运行"""
        pid = self._read_pid()
        if pid <= 0:
            return False
        return _is_process_alive(pid)

    def start(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        redirect_stdout: bool = True,
        redirect_stderr: bool = True,
    ) -> DaemonInfo:
        """启动后台 daemon

        Args:
            command: 要执行的命令列表
            env: 环境变量（None 时继承当前环境）
            redirect_stdout: 是否重定向 stdout 到日志
            redirect_stderr: 是否重定向 stderr 到日志

        Returns:
            DaemonInfo

        Raises:
            RuntimeError: daemon 已在运行或启动失败
        """
        if self.is_running():
            raise RuntimeError(f"Daemon already running (pid {self._read_pid()})")

        self._daemon_dir.mkdir(parents=True, exist_ok=True)

        # 构建环境
        run_env = {**os.environ}
        if env:
            run_env.update(env)

        # 准备输出文件
        out_fh = None
        if redirect_stdout or redirect_stderr:
            out_fh = open(self.logfile, "a", encoding="utf-8")

        try:
            proc = subprocess.Popen(
                command,
                env=run_env,
                stdout=out_fh or subprocess.DEVNULL,
                stderr=out_fh or subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # 脱离当前进程组
                creationflags=get_creation_flags(),
            )
        except Exception as exc:
            if out_fh:
                out_fh.close()
            raise RuntimeError(f"Failed to start daemon: {exc}") from exc

        # 写入 pidfile
        self._write_pid(proc.pid)

        info = DaemonInfo(
            pid=proc.pid,
            started_at=_now_iso(),
            workspace=str(self._workspace),
            status="running",
        )
        logger.info("Daemon started: pid=%d, cmd=%s", proc.pid, " ".join(command))
        return info

    def stop(self, *, force: bool = False, timeout: float = 10.0) -> bool:
        """停止 daemon

        Args:
            force: 是否强制杀死进程
            timeout: 等待优雅退出的超时（秒）

        Returns:
            是否成功停止
        """
        pid = self._read_pid()
        if pid <= 0:
            return True  # 没有运行

        if not _is_process_alive(pid):
            self._clear_pid()
            return True

        try:
            if force:
                _kill_process(pid, force=True)
            else:
                _kill_process(pid)

            # 等待退出
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not _is_process_alive(pid):
                    break
                time.sleep(0.5)
            else:
                # 超时，强制杀死
                _kill_process(pid, force=True)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            logger.error("Permission error stopping daemon: %s", exc)
            return False

        # 等待文件句柄释放（Windows 需要）
        time.sleep(0.3)
        self._clear_pid()
        logger.info("Daemon stopped: pid=%d", pid)
        return True

    def restart(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> DaemonInfo:
        """重启 daemon"""
        self.stop()
        time.sleep(0.5)
        return self.start(command, env=env)

    def get_info(self) -> DaemonInfo:
        """获取 daemon 运行信息"""
        pid = self._read_pid()
        if pid <= 0:
            return DaemonInfo(status="stopped")

        alive = _is_process_alive(pid)
        if not alive:
            self._clear_pid()
            return DaemonInfo(status="stopped")

        started_at = ""
        pidfile = self.pidfile
        if pidfile.exists():
            try:
                data = json.loads(pidfile.read_text(encoding="utf-8"))
                started_at = data.get("started_at", "")
            except Exception:
                pass

        uptime = 0.0
        if started_at:
            try:
                started = datetime.fromisoformat(started_at)
                uptime = (datetime.now(timezone.utc) - started).total_seconds()
            except Exception:
                pass

        return DaemonInfo(
            pid=pid,
            started_at=started_at,
            workspace=str(self._workspace),
            status="running",
            uptime_seconds=uptime,
        )

    def get_log_tail(self, lines: int = 50) -> str:
        """获取日志尾部"""
        if not self.logfile.exists():
            return ""
        try:
            all_lines = self.logfile.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(all_lines[-lines:])
        except Exception:
            return ""

    # ============================================================
    # pidfile 操作
    # ============================================================

    def _read_pid(self) -> int:
        if not self.pidfile.exists():
            return -1
        try:
            data = json.loads(self.pidfile.read_text(encoding="utf-8"))
            return int(data.get("pid", 0))
        except Exception:
            return -1

    def _write_pid(self, pid: int) -> None:
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "started_at": _now_iso(),
            "workspace": str(self._workspace),
        }
        self.pidfile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _clear_pid(self) -> None:
        try:
            self.pidfile.unlink()
        except FileNotFoundError:
            pass


# ============================================================
# 辅助函数
# ============================================================

def _kill_process(pid: int, force: bool = False) -> None:
    """终止进程（跨平台）"""
    if sys.platform == "win32":
        sig = signal.SIGTERM
    else:
        sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _is_process_alive(pid: int) -> bool:
    """检查进程是否存在"""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_creation_flags() -> int:
    """获取 Windows 进程创建标志"""
    if sys.platform == "win32":
        import subprocess
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return 0
