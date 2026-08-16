import hashlib
import json
import os
import os.path
import re
import stat
import shutil
import sys
import zipfile
from configparser import ConfigParser
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from utils.configs import configs
from utils.net_tools import requests_urllib
try:
    from utils.release_info import load_release_channel
except (ImportError, AttributeError):
    def load_release_channel():
        """Keep source/older installs on the historical beta update channel."""
        return 'beta'


REPOSITORY = 'hope140/embyToLocalPlayer'
RELEASES_API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=100'
CHANNEL_ASSETS = {
    'beta': {
        'package': 'etlp-remote-control-beta.zip',
        'checksum': 'etlp-remote-control-beta.zip.sha256',
    },
    'stable': {
        'package': 'etlp-remote-control-stable.zip',
        'checksum': 'etlp-remote-control-stable.zip.sha256',
    },
}
# New releases may publish this optional metadata asset alongside the package
# and checksum. Historical releases do not have it and remain valid.
MANIFEST_ASSET = 'release-plan.json'
# Compatibility default for callers that used the old parser directly. New
# downloads always pass the selected channel's asset explicitly.
PACKAGE_ASSET = CHANNEL_ASSETS['beta']['package']
CONFIG_PREFIX = 'embyToLocalPlayer_config'

_CHECKSUM_RECORD = re.compile(r'(?P<digest>[0-9a-fA-F]{64}) {2}(?P<filename>\S+)')
_RELEASE_TAG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+/\-]{0,127}$')
_UPDATE_CDN_PLACEHOLDER = '{url}'


def _normalise_channel(channel):
    if channel is None:
        channel = load_release_channel()
    if not isinstance(channel, str) or channel not in CHANNEL_ASSETS:
        raise ValueError(f'unsupported release channel: {channel!r}')
    return channel


def _release_download_url(tag, asset_name):
    if not isinstance(tag, str) or _RELEASE_TAG.fullmatch(tag) is None:
        raise ValueError(f'unsupported GitHub release tag: {tag!r}')
    return f'https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe="")}/{quote(asset_name, safe="")}'


def _validate_update_cdn_url(update_cdn_url):
    """Validate an optional HTTPS URL template used for updater requests."""
    if update_cdn_url is None:
        return None
    if not isinstance(update_cdn_url, str):
        raise ValueError('update_cdn_url must be a string')
    if update_cdn_url == '':
        return None

    placeholder_count = update_cdn_url.count(_UPDATE_CDN_PLACEHOLDER)
    if placeholder_count != 1:
        raise ValueError('update_cdn_url must contain exactly one {url} placeholder')
    if any(character.isspace() for character in update_cdn_url):
        raise ValueError('update_cdn_url must be a valid HTTPS URL template')

    try:
        parsed = urlsplit(update_cdn_url)
    except ValueError as exc:
        raise ValueError('update_cdn_url must be a valid HTTPS URL template') from exc
    if parsed.scheme.casefold() != 'https' or not parsed.netloc:
        raise ValueError('update_cdn_url must be an absolute HTTPS URL template')
    if _UPDATE_CDN_PLACEHOLDER in parsed.netloc:
        raise ValueError('update_cdn_url placeholder must be outside the URL authority')
    return update_cdn_url


def _apply_update_cdn_url(url, update_cdn_url=None):
    """Return ``url`` directly or apply the validated CDN URL template."""
    update_cdn_url = _validate_update_cdn_url(update_cdn_url)
    if update_cdn_url is None:
        return url
    return update_cdn_url.replace(_UPDATE_CDN_PLACEHOLDER, url)


def select_latest_release(releases, channel):
    """Select the newest published release carrying the channel assets."""
    channel = _normalise_channel(channel)
    if not isinstance(releases, list):
        raise ValueError('GitHub releases response must be a JSON array')

    assets = CHANNEL_ASSETS[channel]
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft') is True:
            continue
        tag = release.get('tag_name')
        if not isinstance(tag, str) or _RELEASE_TAG.fullmatch(tag) is None:
            continue
        if channel == 'beta' and not tag.endswith('-beta'):
            continue
        if channel == 'stable' and (tag.endswith('-beta') or release.get('prerelease') is True):
            continue

        release_assets = release.get('assets')
        if not isinstance(release_assets, list):
            continue
        asset_names = {
            asset.get('name')
            for asset in release_assets
            if isinstance(asset, dict) and isinstance(asset.get('name'), str)
        }
        if assets['package'] not in asset_names or assets['checksum'] not in asset_names:
            continue

        published_at = release.get('published_at') or release.get('created_at') or ''
        if not isinstance(published_at, str):
            published_at = ''
        release_id = release.get('id')
        if not isinstance(release_id, int):
            release_id = 0
        candidates.append((published_at, release_id, tag, asset_names))

    if not candidates:
        raise ValueError(f'no published {channel} release with matching assets was found')

    _, _, tag, asset_names = max(candidates, key=lambda candidate: candidate[:3])
    selected = {
        'channel': channel,
        'tag': tag,
        'package_asset': assets['package'],
        'checksum_asset': assets['checksum'],
        'update_url': _release_download_url(tag, assets['package']),
        'checksum_url': _release_download_url(tag, assets['checksum']),
    }
    if MANIFEST_ASSET in asset_names:
        selected.update(
            {
                'manifest_asset': MANIFEST_ASSET,
                'manifest_url': _release_download_url(tag, MANIFEST_ASSET),
            }
        )
    return selected


def resolve_update_urls(channel=None, update_cdn_url=None):
    """Resolve package URLs for the installed channel, optionally via a CDN."""
    channel = _normalise_channel(channel)
    update_cdn_url = _validate_update_cdn_url(update_cdn_url)
    releases = requests_urllib(
        _apply_update_cdn_url(RELEASES_API_URL, update_cdn_url),
        get_json=True,
        headers={'Accept': 'application/vnd.github+json'},
        timeout=10,
        retry=3,
    )
    if isinstance(releases, str):
        try:
            releases = json.loads(releases)
        except json.JSONDecodeError as exc:
            raise ValueError('GitHub releases response is not valid JSON') from exc
    release = select_latest_release(releases, channel)
    if update_cdn_url is None:
        return release
    resolved = {
        **release,
        'update_url': _apply_update_cdn_url(release['update_url'], update_cdn_url),
        'checksum_url': _apply_update_cdn_url(release['checksum_url'], update_cdn_url),
    }
    if 'manifest_url' in release:
        resolved['manifest_url'] = _apply_update_cdn_url(release['manifest_url'], update_cdn_url)
    return resolved


def parse_checksum(checksum_text, expected_asset=PACKAGE_ASSET):
    """Parse the one-record sidecar for the selected package asset."""
    if isinstance(checksum_text, bytes):
        try:
            checksum_text = checksum_text.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('checksum file is not valid UTF-8 text') from exc
    if not isinstance(checksum_text, str):
        raise ValueError('checksum response must be text')

    records = [line for line in checksum_text.splitlines() if line.strip()]
    if len(records) != 1:
        raise ValueError(
            f'checksum file must contain exactly one non-empty record; found {len(records)}')

    match = _CHECKSUM_RECORD.fullmatch(records[0])
    if match is None:
        raise ValueError('checksum record must contain 64 hex characters, two spaces, and a filename')
    filename = match.group('filename')
    if filename != expected_asset:
        raise ValueError(f'checksum filename does not match expected asset {expected_asset!r}')
    return match.group('digest').lower()


def calculate_sha256(path, chunk_size=1024 * 1024):
    """Return a file's SHA256 digest while reading it in bounded chunks."""
    if chunk_size <= 0:
        raise ValueError('SHA256 chunk size must be positive')
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_manifest(
    manifest_text,
    *,
    channel,
    tag,
    package_asset,
    checksum_asset,
    expected_digest,
):
    """Validate optional release metadata and return its parsed fields."""
    if isinstance(manifest_text, bytes):
        try:
            manifest_text = manifest_text.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('release manifest is not valid UTF-8 text') from exc
    if not isinstance(manifest_text, str):
        raise ValueError('release manifest response must be text')

    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise ValueError('release manifest is not valid JSON') from exc
    if not isinstance(manifest, dict):
        raise ValueError('release manifest must be a JSON object')

    if type(manifest.get('schema')) is not int or manifest['schema'] != 1:
        raise ValueError('release manifest schema must be 1')
    if manifest.get('channel') != channel:
        raise ValueError('release manifest channel does not match selected channel')
    if manifest.get('version') != tag:
        raise ValueError('release manifest version does not match release tag')
    if manifest.get('branch') != channel:
        raise ValueError('release manifest branch does not match selected channel')
    if manifest.get('packageAsset') != package_asset:
        raise ValueError('release manifest packageAsset does not match selected asset')
    if manifest.get('checksumAsset') != checksum_asset:
        raise ValueError('release manifest checksumAsset does not match selected asset')

    manifest_digest = manifest.get('packageSha256')
    if not isinstance(manifest_digest, str) or re.fullmatch(r'[0-9a-fA-F]{64}', manifest_digest) is None:
        raise ValueError('release manifest packageSha256 must contain 64 hexadecimal characters')
    manifest_digest = manifest_digest.lower()
    if manifest_digest != expected_digest:
        raise ValueError('release manifest packageSha256 does not match checksum sidecar')

    package_size = manifest.get('packageSize')
    if isinstance(package_size, bool) or not isinstance(package_size, int) or package_size < 0:
        raise ValueError('release manifest packageSize must be a non-negative integer')

    return {
        'package_sha256': manifest_digest,
        'package_size': package_size,
    }


def download_verified_update(cwd, channel=None, update_cdn_url=None):
    """Download and verify the update archive, returning its live archive path."""
    cwd = Path(cwd)
    zip_path = cwd / 'embyToLocalPlayer.zip'
    zip_part_path = Path(f'{zip_path}.part')
    try:
        release = resolve_update_urls(channel, update_cdn_url=update_cdn_url)
        checksum_text = requests_urllib(release['checksum_url'], decode=True)
        expected_digest = parse_checksum(checksum_text, release['package_asset'])
        manifest = None
        if release.get('manifest_url'):
            manifest_text = requests_urllib(release['manifest_url'], decode=True)
            manifest = _validate_release_manifest(
                manifest_text,
                channel=release['channel'],
                tag=release['tag'],
                package_asset=release['package_asset'],
                checksum_asset=release['checksum_asset'],
                expected_digest=expected_digest,
            )

        requests_urllib(release['update_url'], save_path=str(zip_part_path))
        if not zip_part_path.is_file():
            raise FileNotFoundError('update archive download did not produce a file')
        if manifest is not None:
            actual_size = zip_part_path.stat().st_size
            if actual_size != manifest['package_size']:
                raise ValueError(
                    'update archive size mismatch: '
                    f"expected {manifest['package_size']}, got {actual_size}"
                )
        actual_digest = calculate_sha256(zip_part_path)
        if actual_digest != expected_digest:
            raise ValueError(
                f'update archive SHA256 mismatch: expected {expected_digest}, got {actual_digest}')

        os.replace(str(zip_part_path), str(zip_path))
        return str(zip_path)
    except Exception:
        try:
            zip_part_path.unlink()
        except OSError:
            pass
        raise

# Keep the updater in lockstep with scripts/package_release.ps1. The release
# asset is the exact runtime archive produced by the channel wrapper, while
# these rules also protect installations if an unexpected extra member is ever
# added.
PACKAGE_ROOT_FILES = frozenset(
    {
        'embyToLocalPlayer.py',
        'embyToLocalPlayer_config.ini',
        'embyToLocalPlayer_debug.bat',
        'LICENSE',
        'requirements.txt',
    }
)


def _is_runtime_member(name):
    """Return whether a flattened archive member belongs to the runtime package."""
    if not name:
        return False
    if '/' not in name:
        return name in PACKAGE_ROOT_FILES

    parts = name.split('/')
    top_level = parts[0]
    basename = parts[-1]
    suffix = Path(basename).suffix.casefold()
    if top_level == 'utils':
        return suffix == '.py' and not any(part.casefold() == 'others' for part in parts[1:-1])
    if top_level == 'user_script':
        return suffix == '.js'
    if top_level == 'third_party':
        return len(parts) == 2 and suffix == '.whl'
    return False


def _normalise_member_name(name):
    """Return a safe, POSIX-style member path or raise on zip-slip input."""
    # Zip files conventionally use '/', but accepting '\\' here would let a
    # Windows archive bypass a component check. Treat it as a separator and
    # validate the resulting path instead.
    name = str(name).replace('\\', '/')
    if not name or name.startswith('/') or name.startswith('//'):
        raise ValueError(f'unsafe absolute zip member: {name!r}')
    if len(name) >= 2 and name[1] == ':' and name[0].isalpha():
        raise ValueError(f'unsafe drive-qualified zip member: {name!r}')

    parts = []
    for part in name.split('/'):
        if not part or part == '.':
            continue
        if part == '..':
            raise ValueError(f'unsafe parent zip member: {name!r}')
        parts.append(part)
    if not parts:
        raise ValueError(f'empty zip member: {name!r}')
    return '/'.join(parts)


def _zip_member_is_symlink(info):
    """Reject Unix symlink entries instead of materialising an unsafe link."""
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _archive_layout(infos):
    """Return normalised names and an optional single GitHub prefix.

    GitHub branch archives contain one directory (for example
    ``embyToLocalPlayer-beta/``); local test archives may already be
    flat. The common prefix is removed before extraction.
    """
    names = []
    for info in infos:
        normalised = _normalise_member_name(info.filename)
        names.append((info, normalised))

    file_names = [name for info, name in names if not info.is_dir()]
    prefix = None
    if file_names:
        first_parts = [name.split('/', 1)[0] for name in file_names]
        common = first_parts[0]
        if all(part == common for part in first_parts) and all('/' in name for name in file_names):
            prefix = common

    result = []
    for info, name in names:
        if prefix is not None:
            if name == prefix:
                # A prefix directory entry has no payload after flattening.
                name = ''
            elif name.startswith(prefix + '/'):
                name = name[len(prefix) + 1:]
            else:
                # A mixed archive is not a valid single-prefix archive. Keep
                # the path, but it still goes through the destination check.
                prefix = None
                break
        result.append((info, name))

    if prefix is None:
        result = [(info, name) for info, name in names]
    return result, prefix


def _safe_destination(root, relative_name):
    """Resolve a member destination and ensure it stays below ``root``."""
    if not relative_name:
        return None
    root_path = Path(root).resolve()
    destination = (root_path / Path(*relative_name.split('/'))).resolve()
    try:
        common = os.path.commonpath((str(root_path), str(destination)))
    except ValueError:
        common = ''
    if common != str(root_path):
        raise ValueError(f'zip member escapes destination: {relative_name!r}')
    return destination


def _config_member(entries):
    candidates = []
    for info, name in entries:
        if not name or info.is_dir():
            continue
        if Path(name).name == 'embyToLocalPlayer_config.ini':
            candidates.append((info, name))
    if not candidates:
        raise ValueError('update archive does not contain the canonical embyToLocalPlayer_config.ini template')
    return candidates[0]


def extract_update_archive(zip_path, destination, ini_example, *, is_windows=None):
    """Safely flatten and extract an update archive.

    Configuration files are never written into the live destination. The
    archive's canonical template is copied to ``ini_example`` instead.
    Returns the detected top-level prefix (or ``None`` for a flat archive).
    """
    destination = Path(destination).resolve()
    ini_example = Path(ini_example).resolve()
    if ini_example.name.startswith(CONFIG_PREFIX):
        raise ValueError('example INI path must not be an embyToLocalPlayer_config* file')
    destination.mkdir(parents=True, exist_ok=True)
    if is_windows is None:
        is_windows = os.name == 'nt'

    with zipfile.ZipFile(zip_path) as archive:
        entries, prefix = _archive_layout(archive.infolist())
        config_info, _ = _config_member(entries)

        # Validate every member before writing anything. This avoids partial
        # extraction if a later entry is malicious or malformed.
        destinations = []
        for info, name in entries:
            if _zip_member_is_symlink(info):
                raise ValueError(f'unsupported symlink zip member: {info.filename!r}')
            if not name:
                continue
            if not _is_runtime_member(name):
                continue
            target = _safe_destination(destination, name)
            if target is not None and not info.is_dir():
                destinations.append((info, name, target))

        for info, name, target in destinations:
            basename = Path(name).name
            if basename.startswith(CONFIG_PREFIX):
                continue
            if is_windows and basename.startswith('etlp_run'):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)

        ini_example.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(config_info) as source, ini_example.open('wb') as output:
            shutil.copyfileobj(source, output)
    return prefix


def check_ini_diff(old_path, new_path, diff_path):
    print('diff checking...')
    old_conf = ConfigParser(allow_no_value=True)
    old_conf.read(old_path, encoding='utf-8-sig')
    new_conf = ConfigParser(allow_no_value=True)
    new_conf.read(new_path, encoding='utf-8-sig')
    diff_conf = ConfigParser(allow_no_value=True)

    have_diff = False
    for new_sect in new_conf.sections():
        new_se_d = new_conf[new_sect]
        if not old_conf.has_section(new_sect):
            diff_conf[new_sect] = new_se_d
            continue

        old_se_d = old_conf[new_sect]
        diff_se_d = {k: v for k, v in new_se_d.items() if k not in old_se_d or v != old_se_d.get(k)}
        if diff_se_d:
            diff_conf[new_sect] = diff_se_d
            have_diff = True

    if have_diff:
        print(f'diff {diff_path}')
        with open(diff_path, 'w', encoding='utf-8-sig') as f:
            diff_conf.write(f)


def main():
    cwd = configs.cwd

    ini_old = configs.path
    ini_example = os.path.join(cwd, 'embyToLocalPlayer_example.ini')
    diff_path = os.path.join(cwd, 'embyToLocalPlayer_diff.ini')
    print('#' * 50)

    print(f'{configs.script_proxy=}')
    update_cdn_url = configs.raw.get('dev', 'update_cdn_url', fallback='')
    print(f'update CDN enabled={bool(update_cdn_url)}')
    print('downloading checksum and archive...')
    zip_path = download_verified_update(cwd, update_cdn_url=update_cdn_url)

    print('unpacking...')
    prefix = extract_update_archive(zip_path, cwd, ini_example)
    # Do not remove caches until verified extraction has completed successfully.
    pycache = os.path.join(cwd, 'utils', '__pycache__')
    shutil.rmtree(pycache, ignore_errors=True)
    print(f'\nnew example {ini_example}; archive prefix={prefix!r}')

    check_ini_diff(old_path=ini_old, new_path=ini_example, diff_path=diff_path)
    print()


if __name__ == '__main__':
    os.chdir(configs.cwd)
    main()
