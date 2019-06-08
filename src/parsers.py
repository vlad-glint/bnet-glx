import logging as log

from definitions import ProductDbInfo, ConfigGameInfo


class ConfigParser(object):
    def __init__(self, config_data):
        self._blizz_code_lang = 'enUS'
        self._region = 'US'
        self.games = []

        if config_data is None:
            return
        try:
            raw_games = self.parse(config_data)
            self.games = self.decode(raw_games)
        except Exception as e:
            log.exception(repr(e))
            log.warning('Failed to read Battle.net config, using default values.')

    @property
    def locale_language(self):
        return self._blizz_code_lang

    @property
    def region(self):
        return self._region

    def parse(self, content):
        for key in content.keys():
            if 'Client' in content[key]:
                self._blizz_code_lang = content[key]['Client']['Language']
            elif 'Services' in content[key]:
                self._region = content[key]['Services']['LastLoginRegion']
        if 'Games' in content:
            return content['Games']
        else:
            return {}

    def decode(self, games_dict):
        games = []
        for uid, properties in games_dict.items():
            if uid == 'battle_net':
                continue
            uninstall_tag = properties.get('ServerUid', None)
            last_played = properties.get('LastPlayed', None)
            games.append(ConfigGameInfo(uid, uninstall_tag, last_played))
        return games


class DatabaseParser(object):
    NOT_GAMES = ('bna', 'agent')
    CLUSTER_SIZE = 128
    PRODUCT_SEPARATOR = 10
    CONTINUATION_MARK = 18  # when long-path product is divided into two clusters

    def __init__(self, data):
        self.data = data
        self.products = {}
        self.parse()

    @property
    def battlenet_present(self):
        return 'bna' in self.products

    @property
    def games(self):
        if self.products:
            return [v for k, v in self.products.items() if k not in self.NOT_GAMES]
        return []

    def parse(self):
        self.products = {}
        offset = 1
        while True:
            if self.data[offset - 1] == 10:  # 0x0A is a section divider
                # extra 128b is for branch md5, not used by plugin right now
                section_size = self.data[offset] + 128 if self.data[offset + 1] == 2 else self.data[offset]
            else:
                break  # very long path = 0x12 or sections end = 0x18
            offset += 2
            section = self.data[offset:offset + section_size]
            offset += section_size + 1
            try:
                product = self._parse_product(section)
            except:
                product = None
            if product:
                self.products[product.ngdp] = product

    def _parse_next(self, section, offset, encoding='utf-8'):
        try:
            size = int.from_bytes(section[offset:offset + 1], 'big')  # path sized always fit in one byte
        except ValueError as e:
            raise RuntimeError('Parsing product.db failed: ' + str(e))
        end = 1 + offset + size
        obj = section[1 + offset:end]
        if encoding:
            obj = obj.decode(encoding)
        return obj, end

    def _skip_unused_sections(self, offset, section):
        t, offset = self._parse_next(section, offset + 1)  # area_code (eu)
        if section[offset + 3] != 0:
            t, offset = self._parse_next(section, offset + 7)  # lang subtitles (enEN)
            t, offset = self._parse_next(section, offset + 1)  # lang voiceover (enEN)
            t, offset = self._parse_next(section, offset + 3)  # lang ??? (plPL)
            while section[offset + 2] != 74:  # loop through unknown_usage languages (enUS)
                t, offset = self._parse_next(section, offset + 5)  # lang
        else:
            # t, offset = self._parse_next(section, offset + 1)  # lang
            offset += 8
        t, offset = self._parse_next(section, offset + 7)  # POL
        t, offset = self._parse_next(section, offset + 1)  # PL
        t, offset = self._parse_next(section, offset + 1)  # internal_name (i.e _retail_)
        # if section[offset] is equal 1 move offset for an extra position
        # remarks: there might be other data in section[offset] so do not add it to offset
        # ...and do it TWICE!
        offset += 2
        if section[offset] == 1:
            offset += 1
        offset += 2
        if section[offset] == 1:
            offset += 1

        return offset

    def _parse_product(self, section):
        uninstall_tag, offset = self._parse_next(section, 1)
        ngdp_code, offset = self._parse_next(section, offset + 1)
        install_path, offset = self._parse_next(section, offset + 3)
        try:
            version, _ = self._parse_next(section, self._skip_unused_sections(offset, section) + 11)
        except:
            version = ''
        return ProductDbInfo(uninstall_tag, ngdp_code, install_path, version)
