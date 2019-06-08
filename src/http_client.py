from definitions import WebsiteAuthData
import logging as log
import pickle
import asyncio

import requests
import requests.cookies
from urllib.parse import urlparse, parse_qs
from functools import partial

from galaxy.api.errors import InvalidCredentials
from galaxy.api.types import Authentication, NextStep
from galaxy.api.jsonrpc import Aborted

from consts import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, FIREFOX_AGENT


def _found_region(cookies):
    try:
        for cookie in cookies:
            if cookie['name'] == 'JSESSIONID':
                _region = cookie['domain'].split('.')[0]
                # 4th region - chinese uses different endpoints, not covered by current plugin
                if _region.lower() in ['eu', 'us', 'kr']:
                    log.debug(f'battle.net region set to: {_region}')
                    return _region
                else:
                    raise ValueError(f'Unknown region {_region}')
        else:  # for
            raise ValueError(f'JSESSIONID cookie not found')
    except Exception as e:
        log.debug(f'battle.net region set to EU, error: {e}')
        return 'eu'


class AuthenticatedHttpClient(object):
    def __init__(self, plugin):
        self._plugin = plugin
        self.user_details = None
        self.region = None
        self.session = None
        self.creds = None
        self.timeout = 10.0
        self.attempted_to_set_battle_tag = None
        self.auth_data = None

    def is_authenticated(self):
        return self.session is not None

    async def shutdown(self):
        await self.session.close()
        self.session = None

    def process_stored_credentials(self, stored_credentials):
        auth_data = WebsiteAuthData(
            cookie_jar=pickle.loads(bytes.fromhex(stored_credentials['cookie_jar'])),
            access_token=stored_credentials['access_token'],
            region=stored_credentials['region'] if 'region' in stored_credentials else 'eu'
        )
        # set default user_details data from cache
        if 'user_details_cache' in stored_credentials:
            self.user_details = stored_credentials['user_details_cache']
            self.auth_data = auth_data
        return auth_data

    async def get_auth_data_login(self, cookie_jar, credentials):
        code = parse_qs(urlparse(credentials['end_uri']).query)["code"][0]
        loop = asyncio.get_running_loop()

        s = requests.Session()
        url = f"https://{self.region}.battle.net/oauth/token"
        data = {
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code
        }
        log.info(f"data {data}")
        response = await loop.run_in_executor(None, partial(s.post, url, data=data))
        response.raise_for_status()
        result = response.json()
        access_token = result["access_token"]
        self.auth_data = WebsiteAuthData(cookie_jar=cookie_jar, access_token=access_token, region=self.region)
        return self.auth_data

    # NOTE: use user data to present usertag/name to Galaxy, if this token expires and plugin cannot refresh it
    # use stored usertag/name if token validation fails, this is temporary solution, as we do not need that
    # endpoint for nothing else at this moment
    def validate_auth_status(self, auth_status):
        if 'error' in auth_status:
            if not self.user_details:
                raise Aborted()
            else:
                log.debug('validate_access_token failed, using stored user_id')
                return False
        elif not self.user_details:
            raise Aborted()
        else:
            if not ("authorities" in auth_status and "IS_AUTHENTICATED_FULLY" in auth_status["authorities"]):
                raise Aborted()
            return True

    def parse_user_details(self):
        log.info(f"oauth/userinfo: {self.user_details}")
        if 'id' and 'battletag' in self.user_details:
            return Authentication(self.user_details["id"], self.user_details["battletag"])
        else:
            raise Aborted()

    def authenticate_using_login(self):
        _URI = f'https://battle.net/oauth/authorize?response_type=code&client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope=wow.profile+sc2.profile'
        auth_params = {
            "window_title": "Login to Battle.net",
            "window_width": 540,
            "window_height": 700,
            "start_uri": _URI,
            "end_uri_regex": r"(.*logout&app=oauth.*)|(^http://friendsofgalaxy\.com.*)"
        }
        return NextStep("web_session", auth_params)

    def parse_auth_after_setting_battletag(self):
        self.creds["user_details_cache"] = self.user_details
        try:
            battletag = self.user_details["battletag"]
        except KeyError:
            log.error("User failed to set battle tag")
            raise InvalidCredentials()
        self._plugin.store_credentials(self.creds)
        return Authentication(self.user_details["id"], battletag)

    def parse_cookies(self, cookies):
        self.region = _found_region(cookies)
        new_cookies = {cookie["name"]: cookie["value"] for cookie in cookies}
        return requests.cookies.cookiejar_from_dict(new_cookies)

    def set_credentials(self):
        self.creds = {"cookie_jar": pickle.dumps(self.auth_data.cookie_jar).hex(), "access_token": self.auth_data.access_token,
                      "user_details_cache": self.user_details, "region": self.auth_data.region}

    def parse_battletag(self):
        try:
            battletag = self.user_details["battletag"]
        except KeyError:
            _URI = f'https://{self.region}.battle.net/login/en/flow/app.app?step=login&ST={self.auth_data.cookie_jar["BA-tassadar"]}&app=app&cr=true'
            log.info(_URI)
            auth_params = {
                "window_title": "Login to Battle.net",
                "window_width": 540,
                "window_height": 700,
                "start_uri": _URI,
                "end_uri_regex": r".*accountName.*"
            }
            self.attempted_to_set_battle_tag = True
            return NextStep("web_session", auth_params)

        self._plugin.store_credentials(self.creds)
        return Authentication(self.user_details["id"], battletag)

    async def create_session(self):
        self.session = requests.Session()
        self.session.cookies = self.auth_data.cookie_jar
        self.region = self.auth_data.region
        self.session.max_redirects = 300
        self.session.headers = {
            "Authorization": f"Bearer {self.auth_data.access_token}",
            "User-Agent": FIREFOX_AGENT
        }

    def refresh_credentials(self):
        creds = {
            "cookie_jar": pickle.dumps(self.session.cookie_jar).hex(),
            "access_token": self.auth_data.access_token
        }

        self._plugin.store_credentials(creds)
