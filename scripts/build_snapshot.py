"""Build the Daytona snapshot every search sandbox starts from.

It holds the engine, the runner and build/search_data.pkl, and nothing else:
no holdout data, no .env, no key of any kind.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402

PACKAGES = ("numpy==2.1.3", "pandas==2.2.3", "pyarrow==18.1.0", "exchange_calendars==4.8")


def posix_archive_paths() -> None:
    """The SDK names each build-context entry after its local path; on Windows that name has
    backslashes, which the generated Dockerfile's COPY drops, so the build cannot find the files."""
    from daytona._sync.object_storage import ObjectStorage

    def compute(path_str) -> str:
        _, rest = os.path.splitdrive(os.path.normpath(str(path_str)))
        return rest.replace("\\", "/").lstrip("/")

    ObjectStorage.compute_archive_base_path = staticmethod(compute)


def image():
    from daytona import Image
    posix_archive_paths()
    img = Image.debian_slim("3.12").pip_install(*PACKAGES).add_local_dir(settings.ENGINE, "/app/engine")
    for name in settings.SANDBOX_FILES:
        img = img.add_local_file(settings.ROOT / "honest_alpha" / name, f"/app/honest_alpha/{name}")
    return img.add_local_file(settings.SEARCH_DATA, "/app/build/search_data.pkl").workdir("/app")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default=None, help="default: named after the code and data it holds")
    ap.add_argument("--cpu", type=int, default=1)
    ap.add_argument("--memory", type=int, default=2, help="GiB")
    ap.add_argument("--disk", type=int, default=3, help="GiB")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")    # build logs carry characters a Windows console code page lacks
    settings.load_env()
    args.name = args.name or settings.snapshot_name()
    from daytona import CreateSnapshotParams, Daytona, Resources

    d = Daytona()
    try:
        snap = d.snapshot.get(args.name)
    except Exception:
        snap = None
    if snap is not None:
        if str(snap.state).lower().endswith("active"):
            print(f"snapshot {args.name} exists: state {snap.state}")
            return 0
        print(f"snapshot {args.name} is {snap.state}; deleting it and building again", flush=True)
        d.snapshot.delete(snap)
        time.sleep(3)
    t0 = time.perf_counter()
    snap = d.snapshot.create(
        CreateSnapshotParams(name=args.name, image=image(),
                             resources=Resources(cpu=args.cpu, memory=args.memory, disk=args.disk)),
        on_logs=lambda line: print(line, flush=True), timeout=0)
    print(f"snapshot {snap.name}: state {snap.state} in {time.perf_counter() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
