#!/usr/bin/env python3
"""Prepare Railway's root-owned volume, then drop privileges before serving."""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

APP_PATH = "/app/server.py"


def chown_tree(path: Path, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(path, followlinks=False):
        try:
            os.chown(root, uid, gid, follow_symlinks=False)
        except FileNotFoundError:
            continue
        for name in dirs + files:
            target = os.path.join(root, name)
            try:
                os.chown(target, uid, gid, follow_symlinks=False)
            except FileNotFoundError:
                pass


def main() -> None:
    if os.geteuid() == 0:
        app = pwd.getpwnam("app")
        volume_path = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")).resolve()
        chown_tree(volume_path, app.pw_uid, app.pw_gid)
        try:
            os.initgroups(app.pw_name, app.pw_gid)
        except AttributeError:
            os.setgroups([app.pw_gid])
        os.setgid(app.pw_gid)
        os.setuid(app.pw_uid)
    os.execv(sys.executable, [sys.executable, APP_PATH])


if __name__ == "__main__":
    main()
