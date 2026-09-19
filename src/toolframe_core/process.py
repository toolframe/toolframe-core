"""Own a worker process, its protocol channel, stderr tail and actual exit."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections import deque

from toolframe_sdk import MAX_FRAME_BYTES, PluginError
from toolframe_sdk.rpc import RpcPeer


class WorkerProcess:
    def __init__(self, process, handler, *, log_callback=None):
        self.process = process
        self.expected_exit = False
        self.logs = deque(maxlen=200)
        self.log_callback = log_callback
        self.peer = RpcPeer(process.stdout, process.stdin, handler)
        self.reader_task = asyncio.create_task(self.peer.run())
        self.log_task = asyncio.create_task(self._logs())

    @classmethod
    async def start(
        cls, python, entrypoint, handler, *, cwd, log_callback=None, host_token=None
    ):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "PYTHONHOME")
        }
        process = await asyncio.create_subprocess_exec(
            str(python),
            "-I",
            "-u",
            "-m",
            "toolframe_sdk.worker",
            "--entrypoint",
            entrypoint,
            *(["--host-token", host_token] if host_token else []),
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME_BYTES,
            start_new_session=True,
        )
        return cls(process, handler, log_callback=log_callback)

    @property
    def pid(self):
        return self.process.pid

    async def _logs(self):
        remainder = b""
        while data := await self.process.stderr.read(4096):
            remainder += data
            lines = remainder.split(b"\n")
            remainder = lines.pop()[-4096:]
            for line in lines:
                text = line[-4096:].decode(errors="replace")
                self.logs.append(text)
                if self.log_callback:
                    self.log_callback(text)

    async def request(self, method, params=None, *, timeout=30):
        return await self.peer.request(method, params, timeout=timeout)

    async def close(self, timeout=10, *, activated=True):
        self.expected_exit = True
        if self.process.returncode is None:
            if activated:
                await self.request("runtime.dispose", timeout=timeout)
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), timeout)
            except TimeoutError as error:
                raise PluginError(
                    "cleanup_failed",
                    "Worker did not exit after disposal",
                    {"pid": self.pid},
                ) from error
        await self.peer.close()
        if group_exists(self.pid):
            raise PluginError(
                "cleanup_failed",
                "Worker process group still has descendants",
                {"pgid": self.pid},
            )
        await asyncio.wait_for(
            asyncio.gather(self.reader_task, self.log_task, return_exceptions=True),
            timeout,
        )
        if self.process.returncode not in (0, None):
            raise PluginError(
                "cleanup_failed",
                "Worker exited unsuccessfully",
                {"returncode": self.process.returncode},
            )

    async def force_stop(self):
        self.expected_exit = True
        await stop_group(self.pid)
        if self.process.returncode is None:
            await asyncio.wait_for(self.process.wait(), 5)
        await self.peer.close()
        await asyncio.wait_for(
            asyncio.gather(self.reader_task, self.log_task, return_exceptions=True), 5
        )


def group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM does not prove absence. Darwin can also return it while the
        # group's last descendants are zombies awaiting collection.
        return True


async def stop_group(pid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if group_exists(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, sig)
            deadline = asyncio.get_running_loop().time() + 2
            while group_exists(pid) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
    if group_exists(pid):
        raise PluginError(
            "cleanup_failed",
            "Process group did not exit after forced stop",
            {"pgid": pid},
        )


async def process_inventory():
    process = await asyncio.create_subprocess_exec(
        "ps", "-axo", "pid=,pgid=,command=", stdout=asyncio.subprocess.PIPE
    )
    output, _ = await process.communicate()
    return {
        int(parts[0]): {"pgid": int(parts[1]), "command": parts[2]}
        for line in output.decode().splitlines()
        if len(parts := line.split(None, 2)) == 3
    }
