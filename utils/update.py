import os
import os.path
import stat
import shutil
import sys
import zipfile
from configparser import ConfigParser
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from utils.configs import configs
from utils.net_tools import requests_urllib


UPDATE_URL = 'https://github.com/hope140/embyToLocalPlayer/archive/refs/heads/beta.zip'
CONFIG_PREFIX = 'embyToLocalPlayer_config'


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
    print('downloading...')
    zip_path = os.path.join(cwd, 'embyToLocalPlayer.zip')
    requests_urllib(UPDATE_URL, save_path=zip_path)

    pycache = os.path.join(cwd, 'utils', '__pycache__')
    shutil.rmtree(pycache, ignore_errors=True)

    print('unpacking...')
    prefix = extract_update_archive(zip_path, cwd, ini_example)
    print(f'\nnew example {ini_example}; archive prefix={prefix!r}')

    check_ini_diff(old_path=ini_old, new_path=ini_example, diff_path=diff_path)
    print()


if __name__ == '__main__':
    os.chdir(configs.cwd)
    main()
