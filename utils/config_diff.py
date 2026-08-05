"""对比本地(我的)配置文件与示例配置，输出全部语义差异。

用法:
    python utils/config_diff.py [我的配置] [示例配置] [-o 报告.txt]

默认:
    我的配置 = embyToLocalPlayer_config.ini
    示例配置 = embyToLocalPlayer_example.ini

比较口径与 utils/update.py 的 check_ini_diff 一致，用 configparser 语义比较：
忽略注释、空行、键顺序和大小写，只比较 section / key / value。
输出三类差异：
    + 仅我的配置有   （本地新增或自定义的键）
    - 仅示例有       （示例新增或本地删除的键）
    ~ 值不同         （两侧取值不同，分别列出）
"""

import argparse
from configparser import ConfigParser
from pathlib import Path


def read_ini(path):
    conf = ConfigParser(allow_no_value=True, interpolation=None)
    conf.read(path, encoding='utf-8-sig')
    return conf


def collect_diffs(local, example):
    """返回 [(section, key, kind, local_value, example_value)]，kind 为
    only_local / only_example / changed 之一。"""
    diffs = []
    sections = sorted(set(local.sections()) | set(example.sections()))
    for sect in sections:
        lo = local[sect] if local.has_section(sect) else {}
        ex = example[sect] if example.has_section(sect) else {}
        for key in sorted(set(lo) | set(ex)):
            lv, ev = lo.get(key), ex.get(key)
            if lv == ev:
                continue
            if key in lo and key not in ex:
                diffs.append((sect, key, 'only_local', lv, None))
            elif key not in lo and key in ex:
                diffs.append((sect, key, 'only_example', None, ev))
            else:
                diffs.append((sect, key, 'changed', lv, ev))
    return diffs


def render(diffs, local_name, example_name):
    lines = []
    lines.append(f'对比: {local_name} (我的)  vs  {example_name} (示例)')
    lines.append('=' * 72)
    counts = {'only_local': 0, 'only_example': 0, 'changed': 0}
    current = None
    for sect, key, kind, lv, ev in diffs:
        counts[kind] += 1
        if sect != current:
            lines.append('')
            lines.append(f'[{sect}]')
            current = sect
        if kind == 'only_local':
            lines.append(f'    + {key} = {lv}   # 仅我的配置有')
        elif kind == 'only_example':
            lines.append(f'    - {key} = {ev}   # 仅示例有')
        else:
            lines.append(f'    ~ {key}')
            lines.append(f'        我的: {lv}')
            lines.append(f'        示例: {ev}')
    lines.append('')
    lines.append(
        f'汇总: 值不同 {counts["changed"]} 项, '
        f'仅我的配置有 {counts["only_local"]} 项, '
        f'仅示例有 {counts["only_example"]} 项'
    )
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='对比我的配置文件与示例配置的全部语义差异'
    )
    parser.add_argument('local', nargs='?', default='embyToLocalPlayer_config.ini',
                        help='我的配置文件路径（默认 embyToLocalPlayer_config.ini）')
    parser.add_argument('example', nargs='?', default='embyToLocalPlayer_example.ini',
                        help='示例配置文件路径（默认 embyToLocalPlayer_example.ini）')
    parser.add_argument('-o', '--out', help='把报告写入指定文件')
    args = parser.parse_args()

    local_path, example_path = Path(args.local), Path(args.example)
    if not local_path.is_file():
        parser.error(f'我的配置文件不存在: {local_path}')
    if not example_path.is_file():
        parser.error(f'示例配置文件不存在: {example_path}')

    local, example = read_ini(local_path), read_ini(example_path)
    report = render(collect_diffs(local, example), local_path, example_path)
    print(report)
    if args.out:
        Path(args.out).write_text(report + '\n', encoding='utf-8')
        print(f'\n报告已写入: {args.out}')


if __name__ == '__main__':
    main()
