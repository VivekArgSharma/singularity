"""Stop a child process and everything it started.

On Windows the venv's python.exe is a small launcher that starts the real
interpreter as a child, so Popen.terminate() only stops the launcher and the
real process keeps running (an old agent would keep calling the API, an old
test engine would keep its port). This kills the whole tree.
"""
import os
import subprocess


def kill_tree(proc, timeout=5.0):
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        pass
