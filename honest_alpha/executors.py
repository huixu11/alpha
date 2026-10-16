"""Where trials run: one Daytona sandbox per trial from the search snapshot, or local processes.

Both run honest_alpha.runner on build/search_data.pkl, so a trial gives the
same numbers wherever it ran. `on_event` gets every state change of every
trial (queued, starting, running, done, failed) for the live view.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from honest_alpha import settings, tracing

_DATA = {}


def _init_worker() -> None:
    from honest_alpha import runner
    _DATA["data"] = runner.load()


def _run_local(item: dict, start: str) -> dict:
    from honest_alpha import runner
    out = runner.run_job({"start": start, "ideas": [item]}, data=_DATA["data"])
    res = out["results"][0]
    res["where"] = {"executor": "local", "pid": out["pid"], "host": out["host"]}
    return res


class LocalExecutor:
    name = "local"

    def __init__(self, workers: int | None = None):
        self.workers = workers or max(1, min(6, (os.cpu_count() or 2) - 1))

    def run(self, items: list[dict], start: str, on_event) -> list[dict]:
        results = []
        for it in items:
            on_event({"trial": it["trial"], "state": "queued"})
        with ProcessPoolExecutor(max_workers=min(self.workers, len(items)), initializer=_init_worker) as pool:
            futures = {pool.submit(_run_local, it, start): it for it in items}
            for it in items:
                on_event({"trial": it["trial"], "state": "running", "where": "local process"})
            for f in as_completed(futures):
                it = futures[f]
                try:
                    res = f.result()
                except Exception as e:
                    res = {"ok": False, "infra": True, "trial": it["trial"], "error": f"{type(e).__name__}: {e}",
                           "where": {"executor": "local"}}
                on_event({"trial": it["trial"], "state": "done" if res.get("ok") else "failed",
                          "seconds": res.get("seconds"), "error": res.get("error")})
                results.append(res)
        return results


class DaytonaExecutor:
    name = "daytona"

    def __init__(self, snapshot: str | None = None, concurrency: int | None = None):
        from daytona import Daytona
        self.client = Daytona()
        self.snapshot = snapshot or settings.snapshot_name()
        try:
            state = str(self.client.snapshot.get(self.snapshot).state).lower()
        except Exception:
            state = "missing"
        if not state.endswith("active"):
            raise RuntimeError(f"Daytona snapshot {self.snapshot} is {state}: run scripts/build_snapshot.py, "
                               f"or use --executor local")
        # 4 x 2 GiB stays under the 10 GiB a Tier 1 organization may run at once
        self.concurrency = concurrency or int(os.environ.get("HONEST_ALPHA_SANDBOXES", "4"))
        self.block_network = os.environ.get("HONEST_ALPHA_SANDBOX_NETWORK", "block") == "block"

    def _create(self, trial: str):
        from daytona import CreateSandboxFromSnapshotParams
        kw = dict(snapshot=self.snapshot, labels={"app": "honest-alpha", "trial": trial},
                  auto_stop_interval=15, ephemeral=True)
        for attempt in range(60):
            try:
                if self.block_network:
                    try:
                        return self.client.create(CreateSandboxFromSnapshotParams(network_block_all=True, **kw),
                                                  timeout=180)
                    except Exception as e:
                        if "network" not in str(e).lower():
                            raise
                        self.block_network = False      # this tier cannot change sandbox networking
                return self.client.create(CreateSandboxFromSnapshotParams(**kw), timeout=180)
            except Exception as e:
                # the organization's CPU or memory is in use by sandboxes still being deleted: wait for room
                if "limit exceeded" not in str(e).lower() or attempt == 59:
                    raise
                time.sleep(3)

    def _one(self, item: dict, start: str, on_event, parent) -> dict:
        trial = item["trial"]
        with tracing.span(f"sandbox {trial}", "TOOL", parent=parent, **{"tool.name": "daytona_backtest",
                          "input.value": json.dumps(item)}) as s:
            on_event({"trial": trial, "state": "starting"})
            t0 = time.perf_counter()
            sb = self._create(trial)
            boot = time.perf_counter() - t0
            try:
                on_event({"trial": trial, "state": "running", "where": sb.id})
                sb.fs.upload_file(json.dumps({"start": start, "ideas": [item]}).encode(), "/tmp/job.json")
                resp = sb.process.exec("python -m honest_alpha.runner /tmp/job.json /tmp/out.json",
                                       cwd="/app", timeout=900)
                if resp.exit_code != 0:
                    raise RuntimeError(f"runner exited {resp.exit_code}: {(resp.result or '')[-1500:]}")
                out = json.loads(sb.fs.download_file("/tmp/out.json"))
            finally:
                try:
                    self.client.delete(sb)
                except Exception:
                    pass
            res = out["results"][0]
            res["where"] = {"executor": "daytona", "sandbox": sb.id, "boot_seconds": round(boot, 2),
                            "load_seconds": out["load_seconds"], "python": out["python"],
                            "network_blocked": self.block_network, "snapshot": self.snapshot}
            tracing.put(s, **{"output.value": json.dumps({k: res.get(k) for k in ("ok", "error", "seconds")}),
                              "daytona.sandbox_id": sb.id, "daytona.boot_seconds": round(boot, 2)})
            return res

    def run(self, items: list[dict], start: str, on_event) -> list[dict]:
        parent = tracing.current()
        results = []
        for it in items:
            on_event({"trial": it["trial"], "state": "queued"})
        with ThreadPoolExecutor(max_workers=min(self.concurrency, len(items))) as pool:
            futures = {pool.submit(self._one, it, start, on_event, parent): it for it in items}
            for f in as_completed(futures):
                it = futures[f]
                try:
                    res = f.result()
                except Exception as e:
                    res = {"ok": False, "infra": True, "trial": it["trial"],
                           "error": f"{type(e).__name__}: {str(e)[:500]}", "where": {"executor": "daytona"}}
                on_event({"trial": it["trial"], "state": "done" if res.get("ok") else "failed",
                          "seconds": res.get("seconds"), "error": res.get("error"),
                          "where": res.get("where", {}).get("sandbox")})
                results.append(res)
        return results


def pick(prefer: str | None = None):
    """Daytona when its key is set (or asked for), else local processes."""
    choice = prefer or os.environ.get("HONEST_ALPHA_EXECUTOR") or ("daytona" if os.environ.get("DAYTONA_API_KEY") else "local")
    return DaytonaExecutor() if choice == "daytona" else LocalExecutor()
