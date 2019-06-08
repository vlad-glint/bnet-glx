from psutil import Process, wait_procs

from definitions import BlizzardGame
from pathfinder import PathFinder
from consts import SYSTEM


pathfinder = PathFinder(SYSTEM)


class InstalledGame(object):
    def __init__(self, info: BlizzardGame, uninstall_tag: str, version: str, last_played: str, install_path: str):
        self.info = info
        self.uninstall_tag = uninstall_tag
        self.version = version
        self.last_played = last_played
        self.install_path = install_path

        self.execs = pathfinder.find_executables(self.install_path)
        self._processes = set()

    @property
    def local_game_args(self):
        return (self.info.blizzard_id, self.is_running)

    @property
    def playable(self):
        if self.version != '':
            return True

    def add_process(self, process: Process):
        if process.exe() in self.execs:
            self._processes.add(process)
        else:
            raise ValueError(f"The process exe [{process.exe()}] doesn't match with the game execs: {self.execs}")

    def is_running(self):
        for process in self._processes:
            if process.is_running():
                return True
        else:
            self._processes = set()
            return False

    def wait_until_game_stops(self, timeout=None):
        wait_procs(self._processes, timeout=timeout, callback=None)
