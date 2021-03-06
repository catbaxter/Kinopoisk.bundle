# -*- coding: utf-8 -*-
from kinoplex.agent import KinoPlex
from kinoplex.const import config
from kinoplex.meta import prepare_meta

from collections import namedtuple
from datetime import datetime
from types import MethodType

from raven.handlers.logging import SentryHandler

import os, time, logging, urllib3, urllib
from urllib3.contrib.socks import SOCKSProxyManager

urllib3.disable_warnings()

# extend HTTPResponse to match Plex network component
class PlexHTTPResponse(urllib3.HTTPResponse):
    def __str__(self):
        return self.data

    @property
    def content(self):
        return self.data

class PlexHTTPConnectionPool(urllib3.HTTPConnectionPool):
    ResponseCls = PlexHTTPResponse

class PlexHTTPSConnectionPool(urllib3.HTTPSConnectionPool):
    ResponseCls = PlexHTTPResponse

def getVersionInfo(core):
    current_version = 'BETA2'
    current_mtime = 0
    version_path = core.storage.join_path(core.bundle_path, 'Contents', 'VERSION')
    if core.storage.file_exists(version_path):
        current_version = core.storage.load(version_path)
        current_mtime = core.storage.last_modified(version_path)
    return current_version, current_mtime

# replace default urllib2 with faster urllib3
def setup_network(core, prefs):
    retries = urllib3.Retry(backoff_factor=2, status_forcelist=set([500]))

    core.networking.pool = urllib3.PoolManager(retries=retries, maxsize=5)
    core.networking.pool.pool_classes_by_scheme = {
        'http': PlexHTTPConnectionPool,
        'https': PlexHTTPSConnectionPool,
    }

    if prefs['proxy_adr'] and prefs['proxy_adr'].startswith('http'):
        if prefs['proxy_type'] == 'SOCK5':
            core.networking.pool_proxy = SOCKSProxyManager(prefs['proxy_adr'], retries=retries, maxsize=5)
        else:
            core.networking.pool_proxy = urllib3.ProxyManager(prefs['proxy_adr'], retries=retries, maxsize=5)
        core.networking.pool_proxy.pool_classes_by_scheme = core.networking.pool.pool_classes_by_scheme

    core.networking.http_request = MethodType(urllib3_http_request, core.networking)

def setup_sentry(core, platform):
    handler = SentryHandler('https://5a974a896d6b4d208ca70d600814d942@sentry.io/202380', tags={
        'os': platform.OS,
        'plexname': core.get_server_attribute('friendlyName'),
        'osversion': platform.OSVersion,
        'cpu': platform.CPU,
        'serverversion': platform.ServerVersion,
        'pluginversion': getVersionInfo(core)[0]
    })
    handler.setLevel(logging.ERROR)
    core.log.addHandler(handler)
    u3 = logging.getLogger("urllib3")
    u3.setLevel(core.log.getEffectiveLevel())
    for ch in core.log.handlers:
        u3.addHandler(ch)

def _content_type_allowed(content_type):
    for t in ['html', 'xml', 'json', 'javascript']:
        if t in content_type:
            return True
    return False

# implement http_request using urllib3
def urllib3_http_request(self, url, values=None, headers={}, cacheTime=None, encoding=None, errors=None, timeout=0, immediate=False, sleep=0, data=None, opener=None, sandbox=None, follow_redirects=True, basic_auth=None, method=None):
    if cacheTime == None: cacheTime = self.cache_time
    pos = url.rfind('#')
    if pos > 0:
        url = url[:pos]

    if values and not data:
        data = urllib.urlencode(values)

    if data:
        cacheTime = 0
        immediate = True

    url_cache = None
    if self._http_caching_enabled:
        if cacheTime > 0:
            cache_mgr = self._cache_mgr
            if cache_mgr.item_count > self._core.config.http_cache_max_items + self._core.config.http_cache_max_items_grace:
                cache_mgr.trim(self._core.config.http_cache_max_size, self._core.config.http_cache_max_items)
            url_cache = cache_mgr[url]
            url_cache.set_expiry_interval(cacheTime)
        else:
            del self._cache_mgr[url]

    if url_cache != None and url_cache['content'] and not url_cache.expired:
        content_type = url_cache.headers.get('Content-Type', '')
        if self._core.plugin_class == 'Agent' and not _content_type_allowed(content_type):
            self._core.log.debug("Removing cached data for '%s' (content type '%s' not cacheable in Agent plug-ins)", url, content_type)
            manager = url_cache._manager
            del manager[url]
        else:
            self._core.log.debug("Fetching '%s' from the HTTP cache", url)
            res = PlexHTTPResponse(url_cache['content'], headers=url_cache.headers)
            return res

    h = dict(self.default_headers)
    h.update({'connection': 'keep-alive'})
    if sandbox:
        h.update(sandbox.custom_headers)
    h.update(headers)

    if 'PLEXTOKEN' in os.environ and len(os.environ['PLEXTOKEN']) > 0 and h is not None and url.find('http://127.0.0.1') == 0:
        h['X-Plex-Token'] = os.environ['PLEXTOKEN']

    if basic_auth != None:
        h['Authorization'] = self.generate_basic_auth_header(*basic_auth)

    if url.startswith(config.kinopoisk.api.base[:-2]):
        h.update({'clientDate': datetime.now().strftime("%H:%M %d.%m.%Y"), 'x-timestamp': str(int(round(time.time() * 1000)))})
        h.update({'x-signature': self._core.data.hashing.md5(url[len(config.kinopoisk.api.base[:-2]):]+h.get('x-timestamp')+config.kinopoisk.api.hash)})

    if url.find('http://127.0.0.1') < 0 and hasattr(self, 'pool_proxy'):
        req = self.pool_proxy.request(method or 'GET', url, headers=h, redirect=follow_redirects, body=data)
    else:
        req = self.pool.request(method or 'GET', url, headers=h, redirect=follow_redirects, body=data)

    if url_cache != None:
        content_type = req.getheader('Content-Type', '')
        if self._core.plugin_class == 'Agent' and not _content_type_allowed(content_type):
            self._core.log.debug("Not caching '%s' (content type '%s' not cacheable in Agent plug-ins)", url, content_type)
        else:
            url_cache['content'] = req.data
            url_cache.headers = dict(req.headers)
    return req

# main search function
def search_event(self, results, media, lang, manual=False, version=0, primary=True):
    self.fire('search', results, media, lang, manual, primary)

# main update function
def update_event(self, metadata, media, lang, force=False, version=0, periodic=False):
    ids = {}
    if self.api.Data.Exists(media.id):
        ids = self.api.Data.LoadObject(media.id)
    metadict = dict(id=metadata.id, meta_ids=ids)
    self.fire('update', metadict, media, lang, force, periodic)
    prepare_meta(metadict, metadata, self.api)
    self.api.Data.SaveObject(media.id, metadict['meta_ids'])

# class constructor
def init_class(cls_name, cls_base, gl, version=0):
    g = dict((k, v) for k, v in gl.items() if not k.startswith("_"))
    d = {
        'name': u'Кинопоиск2.0',
        'api': namedtuple('Struct', g.keys())(*g.values()),
        'agent_type': 'movies' if cls_base.__name__ == 'Movies' else 'series',
        'primary_provider': True,
        'languages': ['ru', 'en'],
        'accepts_from': ['com.plexapp.agents.localmedia'],
        'contributes_to': config.get('contrib',{}).get(cls_base.__name__,[]),
        'c': config,
        #'s': filter(lambda x: x.__class__.__name__ == 'SentryHandler', g['Core'].log.handlers)[0].client,
        'search': search_event,
        'update': update_event
    }
    return d.get('__metaclass__', type)(cls_name, (KinoPlex, cls_base,), d)