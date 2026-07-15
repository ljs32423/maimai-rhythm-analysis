#!/usr/bin/env python3
"""
maimai 节奏解析 一键生成
========================
按顺序自动执行六个步骤:
  1. init_meter     — 生成默认 4/4 拍号文件
  2. visualize      — 用拍号时间轴生成节奏解析图片 (SVG + PNG)
  3. render_preview — 从 maidata.txt 生成缺失的谱面预览视频 (MajdataView)
  4. align_audio    — 自动对齐视频音轨与谱面 (首个 tap 检测)
  5. make_html      — 生成网页版 (谱面预览视频 + 滚动节奏条)
  6. export_player  — 导出原生桌面播放器 manifest/scene

各步骤相互独立，某步失败不会阻断后续步骤。
用法:
  python run_all.py                            # 批量所有歌曲 (默认 MASTER/Re:MASTER)
  python run_all.py -d "WiPE OUT MEMORIES"     # 单曲
  python run_all.py -diff 4                    # 指定难度
  python run_all.py -f                         # 只强制重建图片和网页
"""
import os, sys, argparse, subprocess
from datetime import datetime
from .difficulty import DIFFICULTY_NAMES, default_target_difficulties
from .simai_parser import parse_maidata
from .song_library import PROJECT_ROOT, discover_song_folders


def timestamp_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def target_difficulties_for_song(song_path, requested_difficulty=None):
    if requested_difficulty is not None:
        return [requested_difficulty]
    maidata = os.path.join(song_path, 'maidata.txt')
    if not os.path.exists(maidata):
        return []
    return default_target_difficulties(parse_maidata(maidata).charts)


def run_step(name, script, args_list, force=False):
    """运行单个步骤的脚本，返回是否成功"""
    if script.endswith('.py'):
        cmd = [sys.executable, script] + args_list
    else:
        cmd = [sys.executable, '-m', script] + args_list
    if force and '-f' not in cmd:
        cmd.append('-f')
    r = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if r.returncode != 0:
        if r.stdout:
            print(r.stdout, end='' if r.stdout.endswith('\n') else '\n')
        if r.stderr:
            print(r.stderr, end='' if r.stderr.endswith('\n') else '\n')
    return r.returncode == 0


def main():
    """主入口：解析参数 → 发现歌曲 → 依次运行 6 个步骤。"""
    ap = argparse.ArgumentParser(description='maimai 节奏解析一键生成')
    ap.add_argument('-i', '--input', default=None, help='歌曲根目录')
    ap.add_argument('-d', '--dir', default=None, help='只处理指定曲目名')
    ap.add_argument('-diff', '--difficulty', type=int, default=None,
                    help='难度 ID；不指定则默认只处理 MASTER/Re:MASTER')
    ap.add_argument(
        '-f', '--force', action='store_true',
        help='强制重建图片和网页；保留已有拍号、视频与音频对齐结果',
    )
    ap.add_argument('-offset', '--offset', type=float, default=0.0, help='初始延迟 (秒)')
    args = ap.parse_args()

    here = str(PROJECT_ROOT)
    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    selected_songs = discover_song_folders(base_dir, args.dir)
    if not selected_songs:
        print(f'在 {base_dir} 下未找到匹配的歌曲')
        return 1

    common = ['-i', args.input] if args.input else []

    # 六个步骤: (名称, 脚本路径, 是否传递 -f, 是否附加 offset)
    # 人工拍号、预览视频和音频对齐结果都不响应 run_all -f。
    steps = [
        ('1/6 拍号文件', 'mra.init_meter', False, False),
        ('2/6 节奏解析图片', 'mra.visualize', True, False),
        ('3/6 谱面预览视频', 'mra.render_preview', False, False),
        ('4/6 音频自动对齐', 'mra.align_audio', False, False),
        ('5/6 网页版预览', 'mra.make_html', True, True),
        ('6/6 桌面播放器数据', 'mra.export_player', True, False),
    ]

    ok_all = True
    for song in selected_songs:
        difficulties = target_difficulties_for_song(song.path, args.difficulty)
        for difficulty in difficulties:
            diff_ok = True
            for name, script, accept_force, needs_offset in steps:
                step_args = common + ['-d', song.path.name, '-diff', str(difficulty)]
                if needs_offset:
                    step_args += ['-offset', str(args.offset)]
                force = args.force and accept_force
                step_ok = run_step(name, script, step_args, force)
                diff_ok = diff_ok and step_ok
                ok_all = ok_all and step_ok
            if diff_ok:
                print(f'[{timestamp_now()}] {song.path.name} {DIFFICULTY_NAMES.get(difficulty, difficulty)} 完成')
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
