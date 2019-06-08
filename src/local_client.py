import re
import os
import asyncio
import logging as log
import shutil
import subprocess
import abc
from time import time

import psutil

from definitions import Blizzard
from process import ProcessProvider
from game import InstalledGame
from consts import Platform, SYSTEM

if SYSTEM == Platform.WINDOWS:
    import winreg
    import ctypes
elif SYSTEM == Platform.MACOS:
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListExcludeDesktopElements


class ClientNotInstalledError(Exception):
    def __init__(self, message="Battle.net not installed", *args, **kwargs):
        super().__init__(message, *args, **kwargs)


class WinUninstaller(object):
    def __init__(self, path):
        self.path = path
        if not os.path.exists(self.path):
            raise FileNotFoundError("Uninstaller not found")

    def uninstall_game(self, game, uninstall_tag, lang):
        args = [
            str(self.path),
            f'--lang={lang}',
            f'--uid={uninstall_tag}',
            f'--displayname={game.info.name}'
        ]
        subprocess.Popen(args, cwd=os.path.dirname(self.path))


class MacUninstaller(object):
    def __init__(self):
        pass

    def uninstall_game(self, game, uninstall_tag, lang):
        log.info(f"INSTALL_PATH: {game.install_path}")
        shutil.rmtree(game.install_path)


class _LocalClient(abc.ABC):
    def __init__(self):
        self._process_provider = ProcessProvider()
        self._process = None
        self._exe = self._find_exe()

    @abc.abstractproperty
    def is_installed(self):
        pass

    @abc.abstractmethod
    def _find_exe(self):
        """Returns Battlenet main executable"""
        pass

    @abc.abstractmethod
    def _is_main_window_open(self):
        """Return True if Blizzard main renderer window is present (main window, not login)"""
        pass

    @abc.abstractmethod
    def _check_for_game_process(self, game):
        """Returns True if process matching game if found"""
        pass

    def refresh(self):
        self._exe = self._find_exe()

    def is_running(self):
        if self._process and self._process.is_running():
            return True
        else:
            self._process = self._process_provider.get_process_by_path(self._exe)
            return bool(self._process)

    async def _prepare_to_launch(self, uid, timeout):
        """launches the client and waits till proper renderer is opened
        :param uid      str of game uid. Makes login window game oriented
        :param timeout  timestamp when a watch should be stopped
        """
        if self.is_running() and self._is_main_window_open():
            return

        subprocess.Popen([self._exe, f'--game={uid}'], cwd=os.path.dirname(self._exe))
        while time() < timeout:
            if self._is_main_window_open():
                log.debug('Preparing to launch ended {:.2f}s before timeout'.format(timeout - time()))
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f'Timeout reached when waiting for gameview from Battle.net')

    def install_game(self, id):
        if not self.is_installed:
            raise ClientNotInstalledError()
        game = Blizzard[id]
        args = [
            self._exe,
            "--install",
            f"--game={game.uid}"
        ]
        subprocess.Popen(args, cwd=os.path.dirname(self._exe))

    async def wait_until_game_stops(self, game: InstalledGame):
        if not self.is_running():
            return 'Client not running'
        for child in self._process.children():
            if child.exe() in game.execs:
                game_process = child
                break
        else:
            return 'No subprocess matches'
        while True:
            if not game_process.is_running():
                return 'Game process is no longer running'
            await asyncio.sleep(1)

    async def launch_game(self, game: InstalledGame, wait_sec):
        if not self.is_installed:
            raise ClientNotInstalledError()
        timeout = time() + wait_sec

        await self._prepare_to_launch(game.info.uid, timeout)

        cmd = f'"{self._exe}" --exec="launch {game.info.family}"'
        subprocess.Popen(cmd, cwd=os.path.dirname(self._exe), shell=True)
        log.info(f"Launch game and start waiting for game process")

        while time() < timeout:
            if self._check_for_game_process(game):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Game process has not appear within {wait_sec}s")


class WinLocalClient(_LocalClient):
    def __init__(self):
        self._WIN_REG_SHELL = (winreg.HKEY_CLASSES_ROOT, r"battlenet\shell\open\command")
        super().__init__()

    def _find_exe(self):
        shell_reg_value = self.__search_registry_for_run_cmd(*self._WIN_REG_SHELL)
        if shell_reg_value is None:
            return None
        reg = re.compile("\"(.*?)\"")  # any chars in double quotes
        return reg.search(shell_reg_value).groups()[0]

    def _find_main_renderer_window(self):
        """Get Blizzard renderer window (main window, not login)
        :return     int number of window; 0 if window not found"""
        return ctypes.windll.user32.FindWindowW(None, "Blizzard Battle.net")

    def _is_main_window_open(self):
        return bool(self._find_main_renderer_window())

    @property
    def is_installed(self):
        return bool(self._exe)

    def close_window(self):
        """Closes Blizzard renderer using native API (but not login window)"""
        bnet_handle = self._find_main_renderer_window()
        ctypes.windll.user32.ShowWindow(bnet_handle, 6)

    def _check_for_game_process(self, game):
        try:
            if not self.is_running():
                return False
            with self._process.oneshot():
                for proc in self._process.children():
                    if proc.exe() in game.execs:
                        log.debug(f'Process has been found')
                        return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        except Exception as e:
            log.error(f'Error while waiting for process to be spawn: {repr(e)}')

    def __search_registry_for_run_cmd(self, *args):
        """
        :param args - arguments as for winreg.OpenKey()
        :returns value of the first string-type key or False if given registry does not exists
        """
        try:
            key = winreg.OpenKey(*args)
            for i in range(1024):
                try:
                    _, exe_cmd, _type = winreg.EnumValue(key, i)
                    if exe_cmd and _type == winreg.REG_SZ:  # null-terminated string
                        return exe_cmd
                except OSError:  # no more data
                    break
        except FileNotFoundError:
            return None


class MacLocalClient(_LocalClient):
    _PATH = "/Applications/Battle.net.app/Contents/MacOS/Battle.net"

    def _find_exe(self):
        return self._PATH

    def _is_main_window_open(self):
        """Main window, not login one"""
        windows = CGWindowListCopyWindowInfo(kCGWindowListExcludeDesktopElements, kCGNullWindowID)
        for window in windows:
            try:
                if 'Blizzard Battle.net' == window['kCGWindowName']:
                    log.debug('Main Battle.net window was found')
                    return True
            except KeyError:
                continue
        return False

    def close_window(self):
        """Not implemented:
            - not possible to get AppKit.NSWindow instance of windows spawned outside this code
            - not possible to run applescript w/o privilage for SystemEvents
        """

    @property
    def is_installed(self):
        return os.path.exists(self._exe)

    def _check_for_game_process(self, game):
        """Check over all processes because on macOS games are spawn not as client children"""
        for proc in psutil.process_iter(attrs=['exe'], ad_value=''):
            if proc.info['exe'] in game.execs:
                return True
        return False


if SYSTEM == Platform.WINDOWS:
    LocalClient = WinLocalClient
    Uninstaller = WinUninstaller
elif SYSTEM == Platform.MACOS:
    LocalClient = MacLocalClient
    Uninstaller = MacUninstaller
