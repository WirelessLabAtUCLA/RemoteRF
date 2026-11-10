"""Private bootstrap: join the owned cgroup before any adapter executable runs.

Run with isolated Python (-I -S). The parent grants execution only after the
child acknowledges cgroup admission. Parent death before that grant closes the
pipe and cannot leave an unrecorded application starting outside the cgroup.
"""
import json
import os
import sys


def main():
    group_fd, ready_fd, grant_fd, env_fd = map(int, sys.argv[1:5])
    try:
        fd = os.open("cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=group_fd)
        try:
            data = str(os.getpid()).encode("ascii")
            if os.write(fd, data) != len(data):
                raise OSError("short cgroup write")
        finally:
            os.close(fd)
        os.close(group_fd)
        os.write(ready_fd, b"R")
        os.close(ready_fd)
        allowed = os.read(grant_fd, 1)
        os.close(grant_fd)
        if allowed != b"G":
            return 125
        with os.fdopen(env_fd, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return 126
        environment = json.loads(raw)
        os.execve(sys.argv[5], sys.argv[5:], environment)
    except BaseException:
        try:
            os.write(ready_fd, b"E")
        except OSError:
            pass
        # Do not copy paths/environment/exception messages into application logs.
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
