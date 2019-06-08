import asyncio
import json
import os
import sys
import multiprocessing
import webbrowser
import requests
import requests.cookies
import pathlib
import logging as log

from version import __version__ as version

from galaxy.api.consts import LocalGameState, Platform
from galaxy.api.errors import AuthenticationRequired, InvalidCredentials, BackendError
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.types import Achievement, Game, LicenseInfo, LocalGame
from galaxy.api.jsonrpc import Aborted

from process import ProcessProvider
from local_client import LocalClient, Uninstaller, ClientNotInstalledError
from parsers import ConfigParser, DatabaseParser
from backend import BackendClient, AccessTokenExpired
from definitions import Blizzard, DataclassJSONEncoder, License_Map
from game import InstalledGame
from watcher import FileWatcher
from consts import CONFIG_PATH, AGENT_PATH, SYSTEM
from consts import Platform as pf
from http_client import AuthenticatedHttpClient


def load_product_db(product_db_path):
    with open(product_db_path, 'rb') as f:
        pdb = f.read()
    return pdb


def load_config(battlenet_config_path):
    with open(battlenet_config_path, 'rb') as f:
        config = json.load(f)
    return config


class BNetPlugin(Plugin):
    PRODUCT_DB_PATH = pathlib.Path(AGENT_PATH) / 'product.db'
    CONFIG_PATH = CONFIG_PATH

    def __init__(self, reader, writer, token):
        super().__init__(Platform.Battlenet, version, reader, writer, token)

        log.info(f"Starting Battle.net plugin, version {version}")

        self.bnet_client = None
        self.local_client = LocalClient()
        self.authentication_client = AuthenticatedHttpClient(self)
        self.backend_client = BackendClient(self, self.authentication_client)
        self.error_state = False

        self.running_task = None

        self.database_parser = None
        self.config_parser = None
        self.uninstaller = None

        self.owned_games_cache = []
        self.installed_games = self._parse_local_data()
        self.watched_running_games = set()

        self.notifications_enabled = False
        loop = asyncio.get_event_loop()
        loop.create_task(self._register_local_data_watcher())

    async def _register_local_data_watcher(self):
        log.info('Registering local data watcher')
        any_change_event = asyncio.Event()
        FileWatcher(self.CONFIG_PATH, any_change_event, interval=1)
        FileWatcher(self.PRODUCT_DB_PATH, any_change_event, interval=2.5)
        while True:
            await any_change_event.wait()
            log.debug('Change in local data detected. Refreshing')
            refreshed_games = self._parse_local_data()
            if not self.notifications_enabled:
                self._update_statuses(refreshed_games, self.installed_games)
            self.installed_games = refreshed_games
            any_change_event.clear()

    async def _notify_about_game_stop(self, game, starting_timeout):
        if game.info.blizzard_id in self.watched_running_games:
            log.debug(f'Game {game.info.blizzard_id} is already watched. Skipping')
            return

        try:
            self.watched_running_games.add(game.info.blizzard_id)
            await asyncio.sleep(starting_timeout)
            ProcessProvider().update_games_processes([game])
            log.info(f'Setuping process watcher for {game._processes}')
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, game.wait_until_game_stops)
        finally:
            self.update_local_game_status(LocalGame(game.info.blizzard_id, LocalGameState.Installed))
            self.watched_running_games.remove(game.info.blizzard_id)

    def _update_statuses(self, refreshed_games, previous_games):
        for blizz_id, refr in refreshed_games.items():
            prev = previous_games.get(blizz_id, None)

            if prev is None:
                if refr.playable:
                    log.debug('Detected playable game')
                    state = LocalGameState.Installed
                else:
                    log.debug('Detected installation begin')
                    state = LocalGameState.None_
            elif refr.playable and not prev.playable:
                log.debug('Detected playable game')
                state = LocalGameState.Installed
            elif refr.last_played != prev.last_played:
                log.debug('Detected launched game')
                state = LocalGameState.Installed | LocalGameState.Running
                asyncio.create_task(self._notify_about_game_stop(refr, 5))
            else:
                continue

            log.info(f'Changing game {blizz_id} state to {state}')
            self.update_local_game_status(LocalGame(blizz_id, state))

        for blizz_id, prev in previous_games.items():
            refr = refreshed_games.get(blizz_id, None)
            if refr is None:
                log.debug('Detected uninstalled game')
                state = LocalGameState.None_
                self.update_local_game_status(LocalGame(blizz_id, state))

    def _parse_local_data(self):
        """Game is considered as installed when present in both config and product.db"""
        games = {}

        try:
            config = load_config(self.CONFIG_PATH)
            self.config_parser = ConfigParser(config)
        except FileNotFoundError as e:
            log.warning(str(e))
            self.config_parser = ConfigParser(None)

        try:
            product_db = load_product_db(self.PRODUCT_DB_PATH)
            self.database_parser = DatabaseParser(product_db)
        except FileNotFoundError as e:
            log.warning('product.db not found:' + str(e))
            return {}
        else:
            if self.local_client.is_installed != self.database_parser.battlenet_present:
                self.local_client.refresh()

        try:
            config = load_config(self.CONFIG_PATH)
            self.config_parser = ConfigParser(config)
        except FileNotFoundError as e:
            log.warning('config file not found:' + str(e))
            self.config_parser = ConfigParser(None)
            return {}

        try:
            if self.uninstaller is None:
                if SYSTEM == pf.WINDOWS:
                    uninstaller_path = pathlib.Path(AGENT_PATH) / 'Blizzard Uninstaller.exe'
                    self.uninstaller = Uninstaller(uninstaller_path)
                elif SYSTEM == pf.MACOS:
                    self.uninstaller = Uninstaller()
        except FileNotFoundError as e:
            log.warning('uninstaller not found' + str(e))

        try:
            if self.local_client.is_installed != self.database_parser.battlenet_present:
                self.local_client.refresh()

            for db_game in self.database_parser.games:
                for config_game in self.config_parser.games:
                    if config_game.uninstall_tag != db_game.uninstall_tag:
                        continue
                    try:
                        blizzard_game = Blizzard[config_game.uid]
                    except KeyError:
                        log.warning(f'[{config_game.uid}] is not known blizzard game. Skipping')
                        continue
                    try:
                        games[blizzard_game.blizzard_id] = InstalledGame(
                            blizzard_game,
                            config_game.uninstall_tag,
                            db_game.version,
                            config_game.last_played,
                            db_game.install_path,
                        )
                    except FileNotFoundError as e:
                        log.warning(str(e) + '. Probably outdated product.db after uninstall. Skipping')
                        continue

        except Exception as e:
            log.exception(str(e))
        finally:
            return games

    def log_out(self):
        if self.backend_client:
            asyncio.create_task(self.authentication_client.shutdown())
        self.authentication_client.user_details = None
        self.owned_games_cache = []

    async def open_battlenet_browser(self):
        url = f"https://www.blizzard.com/apps/battle.net/desktop"
        log.info(f'Opening battle.net website: {url}')
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda x: webbrowser.open(x, autoraise=True), url)

    async def install_game(self, game_id):
        if not self.authentication_client.is_authenticated():
            raise AuthenticationRequired()
        try:
            self.local_client.refresh()
            log.info(f'Installing game of id {game_id}')
            self.local_client.install_game(game_id)
        except ClientNotInstalledError as e:
            log.warning(e)
            await self.open_battlenet_browser()
        except Exception as e:
            log.exception(f"Installing game {game_id} failed: {e}")

    async def uninstall_game(self, game_id):
        if not self.authentication_client.is_authenticated():
            raise AuthenticationRequired()
        if self.uninstaller is None:
            raise FileNotFoundError('Uninstaller not found')
        try:
            installed_game = self.installed_games.get(game_id, None)
            if installed_game is None or not os.access(installed_game.install_path, os.F_OK):
                log.error(f'Cannot uninstall {Blizzard[game_id].uid}')
                self.update_local_game_status(LocalGame(game_id, LocalGameState.None_))
                return

            uninstall_tag = installed_game.uninstall_tag
            client_lang = self.config_parser.locale_language
            self.uninstaller.uninstall_game(installed_game, uninstall_tag, client_lang)
            if SYSTEM == pf.WINDOWS:
                # we're watching config for updates
                pass
            elif SYSTEM == pf.MACOS:
                # config info isn't updated but we are sure that we manually cleaned up the game
                game_state = LocalGameState.None_
                self.update_local_game_status(LocalGame(game_id, game_state))

        except Exception as e:
            log.exception(f'Uninstalling game {game_id} failed: {e}')

    async def launch_game(self, game_id):
        if not self.authentication_client.is_authenticated():
            raise AuthenticationRequired()

        try:
            if self.installed_games is None:
                raise ClientNotInstalledError(message="B.net client is not called or get_local_games not called")

            game = self.installed_games.get(game_id, None)
            if game is None:
                log.error(f'Launching game that is not installed: {game_id}')
                return

            self.local_client.refresh()
            log.info(f'Launching game of id: {game_id}, {game}')
            await self.local_client.launch_game(game, wait_sec=60)

            self.update_local_game_status(LocalGame(game_id, LocalGameState.Installed | LocalGameState.Running))
            self.local_client.close_window()
            asyncio.create_task(self._notify_about_game_stop(game, 3))

        except ClientNotInstalledError as e:
            log.warning(e)
            await self.open_battlenet_browser()
        except TimeoutError as e:
            log.warning(str(e))
        except Exception as e:
            log.exception(f"Launching game {game_id} failed: {e}")

    async def authenticate(self, stored_credentials=None):
        log.info(f"stored_credentials {json.dumps(stored_credentials, indent=4)}")
        try:
            if stored_credentials:
                log.info(f"Authenticate: got stored_credentials {json.dumps(stored_credentials, indent=4)}")
                auth_data = self.authentication_client.process_stored_credentials(stored_credentials)
                try:
                    await self.authentication_client.create_session()
                    await self.backend_client.refresh_cookies()
                    auth_status = await self.backend_client.validate_access_token(auth_data.access_token)
                except Exception as e:
                    log.exception(f"err: {str(e)}")
                    raise Aborted()
                if self.authentication_client.validate_auth_status(auth_status):
                    self.authentication_client.user_details = await self.backend_client.get_user_info()
                return self.authentication_client.parse_user_details()
            else:
                log.info(f"Authenticate: running CEF Authenticator")
                return self.authentication_client.authenticate_using_login()
        except Aborted:
            raise
        except Exception as e:
            log.exception(f"EX: {str(e)}")
            raise InvalidCredentials()

    async def pass_login_credentials(self, step, credentials, cookies):
        log.info(f"end uri, {credentials['end_uri']}")

        if "logout&app=oauth" in credentials['end_uri']:
            # 2fa expired, repeat authentication
            return self.authentication_client.authenticate_using_login()

        if self.authentication_client.attempted_to_set_battle_tag:
            self.authentication_client.user_details = await self.backend_client.get_user_info()
            return self.authentication_client.parse_auth_after_setting_battletag()

        cookie_jar = self.authentication_client.parse_cookies(cookies)
        auth_data = await self.authentication_client.get_auth_data_login(cookie_jar, credentials)

        try:
            await self.authentication_client.create_session()
            await self.backend_client.refresh_cookies()
        except Exception as e:
            log.exception(f"err: {str(e)}")
            raise Aborted()

        auth_status = await self.backend_client.validate_access_token(auth_data.access_token)
        if not ("authorities" in auth_status and "IS_AUTHENTICATED_FULLY" in auth_status["authorities"]):
            raise Aborted()

        self.authentication_client.user_details = await self.backend_client.get_user_info()

        self.authentication_client.set_credentials()

        return self.authentication_client.parse_battletag()

    async def get_owned_games(self):
        if not self.authentication_client.is_authenticated():
            raise AuthenticationRequired()

        try:
            if not self.owned_games_cache:
                games = await self.backend_client.get_owned_games()
                self.owned_games_cache = games["gameAccounts"]
            log.info(json.dumps(self.owned_games_cache, indent=4))
            return [
                Game(
                    str(game["titleId"]),
                    game["localizedGameName"],
                    [],
                    LicenseInfo(License_Map[game["gameAccountStatus"]]),
                )
                for game in self.owned_games_cache
            ]
        except Exception as e:
            log.exception(f"failed to get owned games: {str(e)}")
            raise

    async def get_local_games(self):
        if not self.local_client.is_installed:
            log.warning("Trying to get local games without Blizzard Battle.net installed.")
            return []

        try:
            local_games = []
            running_games = ProcessProvider().update_games_processes(self.installed_games.values())
            for id_, game in self.installed_games.items():
                if game.playable:
                    state = LocalGameState.Installed
                    if id_ in running_games:
                        state |= LocalGameState.Running
                else:
                    state = LocalGameState.None_
                local_games.append(LocalGame(id_, state))

            return local_games

        except Exception as e:
            log.exception(f"failed to get local games: {str(e)}")
            raise

        finally:
            self.enable_notifications = True

    async def _get_wow_achievements(self):
        achievements = []
        try:
            characters_data = await self.backend_client.get_wow_character_data()
            characters_data = characters_data["characters"]

            wow_character_data = await asyncio.gather(
                *[
                    self.backend_client.get_wow_character_achievements(character["realm"], character["name"])
                    for character in characters_data
                ],
                return_exceptions=True,
            )

            for data in wow_character_data:
                if isinstance(data, requests.Timeout) or isinstance(data, requests.ConnectionError):
                    raise data

            wow_achievement_data = [
                list(
                    zip(
                        data["achievements"]["achievementsCompleted"],
                        data["achievements"]["achievementsCompletedTimestamp"],
                    )
                )
                for data in wow_character_data
                if type(data) is dict
            ]

            already_in = set()

            for char_ach in wow_achievement_data:
                for ach in char_ach:
                    if ach[0] not in already_in:
                        achievements.append(Achievement(achievement_id=ach[0], unlock_time=int(ach[1] / 1000)))
                        already_in.add(ach[0])
        except (AccessTokenExpired, BackendError) as e:
            log.exception(str(e))
        with open('wow.json', 'w') as f:
            f.write(json.dumps(achievements, cls=DataclassJSONEncoder))
        return achievements

    async def _get_sc2_achievements(self):
        account_data = await self.backend_client.get_sc2_player_data(self.authentication_client.user_details["id"])

        # TODO what if more sc2 accounts?
        assert len(account_data) == 1
        account_data = account_data[0]

        profile_data = await self.backend_client.get_sc2_profile_data(
                                                         account_data["regionId"], account_data["realmId"],
                                                         account_data["profileId"]
                                                         )

        sc2_achievement_data = [
            Achievement(achievement_id=achievement["achievementId"], unlock_time=achievement["completionDate"])
            for achievement in profile_data["earnedAchievements"]
            if achievement["isComplete"]
        ]

        with open('sc2.json', 'w') as f:
            f.write(json.dumps(sc2_achievement_data, cls=DataclassJSONEncoder))
        return sc2_achievement_data

    # async def get_unlocked_achievements(self, game_id):
    #     if not self.website_client.is_authenticated():
    #         raise AuthenticationRequired()
    #     try:
    #         if game_id == "21298":
    #             return await self._get_sc2_achievements()
    #         elif game_id == "5730135":
    #             return await self._get_wow_achievements()
    #         else:
    #             return []
    #     except requests.Timeout:
    #         raise BackendTimeout()
    #     except requests.ConnectionError:
    #         raise NetworkError()
    #     except Exception as e:
    #         log.exception(str(e))
    #         return []

    async def _tick_runner(self):
        if not self.bnet_client:
            return
        try:
            self.error_state = await self.bnet_client.tick()
        except Exception as e:
            self.error_state = True
            log.exception(f"error state: {str(e)}")
            raise

    def tick(self):
        if not self.error_state and (not self.running_task or self.running_task.done()):
            self.running_task = asyncio.create_task(self._tick_runner())
        elif self.error_state:
            sys.exit(1)

    def shutdown(self):
        log.info("Plugin shutdown.")


def main():
    multiprocessing.freeze_support()
    create_and_run_plugin(BNetPlugin, sys.argv)


if __name__ == "__main__":
    main()
