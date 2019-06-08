import dataclasses as dc
import json
import requests
from typing import Optional
from galaxy.api.consts import LicenseType

License_Map = {
    None: LicenseType.Unknown,
    "Trial": LicenseType.SinglePurchase,
    "Good": LicenseType.SinglePurchase,
    "Inactive": LicenseType.SinglePurchase,
    "Banned": LicenseType.SinglePurchase
}

class DataclassJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if dc.is_dataclass(o):
            return dc.asdict(o)
        return super().default(o)


@dc.dataclass
class WebsiteAuthData(object):
    cookie_jar: requests.cookies.RequestsCookieJar()
    access_token: str
    region: str


@dc.dataclass
class BlizzardGame(object):
    uid: str
    name: str
    blizzard_id: str
    family: str


@dc.dataclass
class ConfigGameInfo(object):
    uid: str
    uninstall_tag: Optional[str]
    last_played: Optional[str]


@dc.dataclass
class ProductDbInfo(object):
    uninstall_tag: str
    ngdp: str = ''
    install_path: str = ''
    version: str = ''


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class _Blizzard(object, metaclass=Singleton):
    _GAMES = [
        BlizzardGame('s1', 'StarCraft', '21297', 'S1'),
        BlizzardGame('s2', 'StarCraft II', '21298', 'S2'),
        BlizzardGame('wow', 'World of Warcraft', '5730135', 'WoW'),
        BlizzardGame('prometheus', 'Overwatch', '5272175', 'Pro'),
        BlizzardGame('w3', 'Warcraft III', '?', 'W3'),
        BlizzardGame('destiny2', 'Destiny 2', '1146311730', 'DST2'),
        BlizzardGame('hs_beta', 'Hearthstone', '1465140039', 'WTCG'),
        BlizzardGame('heroes', 'Heroes of the Storm', '1214607983', 'Hero'),
        BlizzardGame('d3cn', '暗黑破壞神III', '?', 'D3CN'),
        BlizzardGame('diablo3', 'Diablo III', '17459', 'D3'),
        BlizzardGame('viper', 'Call of Duty: Black Ops 4', '1447645266', 'VIPR'),
    ]

    def __init__(self):
        self.__games = {}
        for game in self._GAMES:
            self.__games[game.blizzard_id] = game

    def __getitem__(self, key):
        for game in self._GAMES:
            if key in [game.blizzard_id, game.uid, game.name]:
                return game
        raise KeyError()

    @property
    def games(self):
        return self.__games


Blizzard = _Blizzard()


