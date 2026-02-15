import os
import sys
import time
import signal
import threading
import subprocess


class HotReloader:
    def __init__(self, watch_dirs=None, extensions=None, interval=1.0):
        self.watch_dirs = watch_dirs or ["."]
        self.extensions = extensions or [".py", ".html", ".css", ".js"]
        self.interval = interval
        self._file_mtimes = {}
        self._process = None

    def _get_files(self):
        files = []
        for watch_dir in self.watch_dirs:
            for root, dirs, filenames in os.walk(watch_dir):
                dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
                for fname in filenames:
                    if any(fname.endswith(ext) for ext in self.extensions):
                        files.append(os.path.join(root, fname))
        return files

    def _snapshot(self):
        snap = {}
        for f in self._get_files():
            try:
                snap[f] = os.path.getmtime(f)
            except OSError:
                pass
        return snap

    def _detect_changes(self):
        current = self._snapshot()
        changed = []

        for f, mtime in current.items():
            if f not in self._file_mtimes or self._file_mtimes[f] != mtime:
                changed.append(f)

        for f in self._file_mtimes:
            if f not in current:
                changed.append(f)

        self._file_mtimes = current
        return changed

    def _start_process(self, script):
        return subprocess.Popen([sys.executable, script])

    def run(self, script):
        self._file_mtimes = self._snapshot()
        self._process = self._start_process(script)

        print(f"\n  \033[1m⚡ PhotonAPI\033[0m hot reload enabled")
        print(f"  \033[2m  Watching {len(self._file_mtimes)} files for changes\033[0m\n")

        try:
            while True:
                time.sleep(self.interval)
                changed = self._detect_changes()
                if changed:
                    short_names = [os.path.basename(f) for f in changed[:3]]
                    print(f"\n  \033[33m↻\033[0m  Changed: {', '.join(short_names)} — restarting...")
                    self._process.terminate()
                    self._process.wait()
                    self._process = self._start_process(script)
        except KeyboardInterrupt:
            print("\n  \033[2mStopping...\033[0m")
            if self._process:
                self._process.terminate()
                self._process.wait()


def run_with_reload(script, watch_dirs=None):
    reloader = HotReloader(watch_dirs=watch_dirs)
    reloader.run(script)
