import fnmatch
import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import AgentConf, CollectorConf
from .filters import should_skip
from .uploader import Uploader

log = logging.getLogger("irs-agent")


class _Handler(FileSystemEventHandler):
    def __init__(self, uploader: Uploader, source_tool: str, glob: str):
        self.uploader = uploader
        self.source_tool = source_tool
        self.glob = glob
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._debounce_s = 1.5
        threading.Thread(target=self._drain, daemon=True).start()

    def _enqueue(self, path: str):
        p = Path(path)
        if not fnmatch.fnmatch(p.name, self.glob):
            return
        if should_skip(p):
            return
        with self._lock:
            self._pending[path] = time.time()

    def on_created(self, e):
        if not e.is_directory:
            self._enqueue(e.src_path)

    def on_modified(self, e):
        if not e.is_directory:
            self._enqueue(e.src_path)

    def on_moved(self, e):
        if not e.is_directory:
            self._enqueue(e.dest_path)

    def _drain(self):
        while True:
            time.sleep(0.5)
            now = time.time()
            ready = []
            with self._lock:
                for p, t in list(self._pending.items()):
                    if now - t >= self._debounce_s:
                        ready.append(p)
                        del self._pending[p]
            for p in ready:
                self.uploader.maybe_upload(Path(p), self.source_tool)


def initial_scan(uploader: Uploader, col: CollectorConf):
    for root in col.paths:
        rp = Path(root).expanduser()
        if not rp.exists():
            continue
        for p in rp.rglob(col.glob):
            if p.is_file() and not should_skip(p):
                uploader.maybe_upload(p, col.name)


def run(conf: AgentConf):
    from . import remote
    remote.start(conf)
    uploader = Uploader(conf)
    obs = Observer()
    for col in conf.collectors:
        for root in col.paths:
            rp = Path(root).expanduser()
            rp.mkdir(parents=True, exist_ok=True)
            handler = _Handler(uploader, col.name, col.glob)
            obs.schedule(handler, str(rp), recursive=True)
            log.info("watching %s for %s (%s)", rp, col.glob, col.name)
        initial_scan(uploader, col)
    obs.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop()
        obs.join()
