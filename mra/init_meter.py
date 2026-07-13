"""为每个目标难度初始化默认 4/4 拍号文件。"""
import argparse
import os
import sys

from .difficulty import DIFFICULTY_NAMES, default_target_difficulties, meter_file_path
from .meter import ensure_meter_file
from .simai_parser import parse_maidata
from .song_library import PROJECT_ROOT, find_song_dirs


def process_song(song_dir, difficulties=None):
    """只创建缺失文件；不读取音频、不检查音符，也不推断拍号。"""
    song = parse_maidata(os.path.join(song_dir, "maidata.txt"))
    selected = (default_target_difficulties(song.charts) if difficulties is None
                else [difficulty for difficulty in difficulties if difficulty in song.charts])
    for difficulty in selected:
        output = meter_file_path(song_dir, difficulty)
        if output.is_file():
            action = "已有拍号文件，跳过"
        else:
            ensure_meter_file(song_dir, difficulty)
            action = "已生成默认 4/4"
        print(f"  {DIFFICULTY_NAMES.get(difficulty, difficulty)}: {action}")
        print(f"    -> {output}")


def main():
    parser = argparse.ArgumentParser(description="生成默认 4/4 拍号文件")
    parser.add_argument("-i", "--input", default=None, help="歌曲根目录")
    parser.add_argument("-d", "--dir", default=None, help="只处理指定曲目名")
    parser.add_argument("-diff", "--difficulty", type=int, default=None, help="难度 ID")
    args = parser.parse_args()
    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    songs = find_song_dirs(base_dir, args.dir)
    if not songs:
        print(f"在 {base_dir} 下未找到含 maidata.txt 的目录")
        return 1
    for song_dir, song_id in songs:
        print(f"[{song_id}] 拍号文件")
        process_song(song_dir, [args.difficulty] if args.difficulty is not None else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
