import os.path
import sys
import time

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
except Exception:
    pass

from utils.configs import configs, MyLogger
from utils.bangumi_sync import api_client_via_stream_url, get_emby_season_watched_ep_key, emby_api_via_fist_ep

logger = MyLogger()

PROVIDERS = ('imdb', 'tvdb', 'tmdb')


def _extract_ids(provider_ids: dict):
    return {k.lower(): v for k, v in (provider_ids or {}).items() if k.lower() in PROVIDERS}


def build_movie_item(ep):
    ids = _extract_ids(ep.get('ProviderIds'))
    if not ids:
        return None
    item = {'ids': ids}
    title = ep.get('OriginalTitle') or ep.get('Name')
    year = ep.get('ProductionYear')
    if title:
        item['title'] = title
    if year:
        item['year'] = year
    return item


def get_series_ids_and_meta(emby, series_id):
    series_info = emby.get_item(series_id)
    ids = _extract_ids(series_info.get('ProviderIds'))
    title = series_info.get('OriginalTitle')
    year = series_info.get('ProductionYear')
    return ids, title, year


def build_show_item_with_eps(emby, eps_data):
    fist_ep = eps_data[0]
    ids, title, year = get_series_ids_and_meta(emby, fist_ep['SeriesId'])
    if not ids:
        logger.info(f'simkl: series not have {PROVIDERS} id, skip')
        return

    if cloud_eps := get_emby_season_watched_ep_key(emby=emby, eps_data=eps_data, full_data=True):
        eps_data[:0] = cloud_eps[2][-20:]
    watched_keys = []
    ep_ids = []
    for ep in eps_data:
        ep_index = ep.get('index') or ep.get('IndexNumber')  # sync via stream 不是 index
        season_num = ep.get('ParentIndexNumber')
        if ep_index is None or season_num is None:
            continue
        ep_key = (season_num, ep_index)
        if ep_key in watched_keys:
            continue
        pvd_ids = {k.lower(): v for k, v in ep['ProviderIds'].items() if k.lower() in PROVIDERS}
        watched_keys.append(ep_key)
        if pvd_ids:
            ep_ids.append({'ids': pvd_ids})
    if not watched_keys:
        logger.info('simkl: no watched episode key found, skip')
        return

    seasons_map = {}
    for (season_num, ep_index) in watched_keys:
        ep_obj = {'number': ep_index}
        seasons_map.setdefault(season_num, []).append(ep_obj)

    seasons = [
        {
            'number': season_num,
            # 单集回传倒是可以附带 pvd_ids，但是不知道为什么多集同时附带就失败。
            'episodes': sorted(episodes, key=lambda e: e['number']),
        }
        for season_num, episodes in sorted(seasons_map.items())
    ]
    show = {'ids': ids, 'seasons': seasons,
            # 动漫需要这个标记才能正确映射到 simkl 的 anidb 记录。
            'use_tvdb_anime_seasons': True}
    if title:
        show['title'] = title
    if year:
        show['year'] = year
    return show, ep_ids


def sync_ep_or_movie_to_simkl(simkl, eps_data, emby):
    eps_data = eps_data if isinstance(eps_data, list) else [eps_data]
    fist_ep = eps_data[0]
    _type = fist_ep['Type'].lower()
    allow = ['episode', 'movie']
    if _type not in allow:
        raise ValueError(f'type not in {allow}')

    movies, shows, ep_ids = [], [], []

    if _type == 'movie':
        item = build_movie_item(fist_ep)
        if item:
            movies.append(item)
        else:
            logger.info(f'simkl: not any {PROVIDERS} id, skip | {fist_ep.get("Name")}')
    elif item := build_show_item_with_eps(emby, eps_data):
        shows.append(item[0])
        ep_ids = item[1]

    if not movies and not shows:
        logger.info('simkl: nothing to sync')
        return

    res = simkl.add_ep_or_movie_to_history(movies=movies, shows=shows)
    success_count = sum(res['added'][key] for key in ('movies', 'shows', 'episodes'))
    del res['added']['statuses']
    logger.info(f'simkl: sync {success_count} item {res}')

    if not success_count and ep_ids:
        res = simkl.add_ep_or_movie_to_history(episodes=ep_ids)
        success_count = sum(res['added'][key] for key in ('movies', 'shows', 'episodes'))
        del res['added']['statuses']
        # 效果一般，疑似 simkl 的单集 tvdb id 等并不齐全。
        logger.info(f'simkl: sync {success_count} item via {len(ep_ids)} pvd_ids {res}')
    return res


def simkl_api_client(received_code=None):
    code_received = bool(received_code)
    from utils.simkl_api import SimklApi
    client_id = configs.raw.get('simkl', 'client_id', fallback='')
    client_secret = configs.raw.get('simkl', 'client_secret', fallback='')
    oauth_code = received_code if received_code else None
    if not all([client_id, client_secret]):
        raise ValueError('simkl: require client_id, client_secret')
    simkl = SimklApi(
        client_id=client_id,
        client_secret=client_secret,
        oauth_code=oauth_code,
        token_file=os.path.join(configs.cwd, 'simkl_token.json'),
        http_proxy=configs.script_proxy,
        code_received=code_received)
    return simkl


def simkl_sync_main(simkl=None, emby=None, eps_data=None, test=False):
    simkl = simkl or simkl_api_client()
    if test:
        simkl.test()
        return simkl
    else:
        if not emby:
            eps_data = eps_data if isinstance(eps_data, list) else [eps_data]
            fist_ep = eps_data[0]
            if fist_ep['server'] != 'plex':
                emby = emby_api_via_fist_ep(fist_ep)
        sync_ep_or_movie_to_simkl(simkl=simkl, eps_data=eps_data, emby=emby)
    return simkl


def simkl_sync_via_stream_url(url):
    emby, item_id, parsed_url = api_client_via_stream_url(url)
    if not emby:
        time.sleep(1)
        return
    if not configs.check_str_match(parsed_url.netloc, 'simkl', 'enable_host', log=True):
        time.sleep(1)
        return
    from utils.trakt_sync import emby_eps_data_generator
    eps_data = emby_eps_data_generator(emby=emby, item_id=item_id)
    simkl = simkl_api_client()
    simkl_sync_main(simkl=simkl, emby=emby, eps_data=eps_data, test=False)
    time.sleep(1)


def run_via_console():
    argv = sys.argv
    logger.info(f'{argv=}')
    if len(argv) == 2:
        simkl_sync_via_stream_url(url=argv[1])


if __name__ == '__main__':
    os.chdir(configs.cwd)
    run_via_console()
