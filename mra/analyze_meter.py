"""命令行 BeatNet+ 纯音频拍号分析器。"""
import argparse
import os
import sys

from .difficulty import DIFFICULTY_NAMES, default_target_difficulties, meter_analysis_path
from .meter import analyze_chart_meter
from .simai_parser import parse_maidata, time_to_beat
from .song_library import PROJECT_ROOT, find_song_dirs


def process_song(song_dir, difficulties=None, force=False):
    song = parse_maidata(os.path.join(song_dir, "maidata.txt"))
    selected = (default_target_difficulties(song.charts) if difficulties is None
                else [difficulty for difficulty in difficulties if difficulty in song.charts])
    for difficulty in selected:
        chart = song.charts[difficulty]
        if not chart.notes:
            continue
        total_beats = time_to_beat(
            max(note.time_sec + note.duration_sec for note in chart.notes),
            chart.bpm_timeline,
        )
        meter_map = analyze_chart_meter(
            song_dir, difficulty, chart, total_beats, song.first_offset, force,
        )
        signatures = ", ".join(
            f"{section['start_beat']:g}:{section['signature']}"
            for section in meter_map.signature_sections()
        )
        print(f"  {DIFFICULTY_NAMES.get(difficulty, difficulty)}: {signatures}")
        print(f"    -> {meter_analysis_path(song_dir, difficulty)}")


def main():
    parser = argparse.ArgumentParser(description="用 BeatNet+ 纯音频分析拍号变化")
    parser.add_argument("-i", "--input", default=None, help="歌曲根目录")
    parser.add_argument("-d", "--dir", default=None, help="只处理指定曲目名")
    parser.add_argument("-diff", "--difficulty", type=int, default=None, help="难度 ID")
    parser.add_argument("-f", "--force", action="store_true", help="重新执行自动分析")
    args = parser.parse_args()
    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    songs = find_song_dirs(base_dir, args.dir)
    if not songs:
        print(f"在 {base_dir} 下未找到含 maidata.txt 的目录")
        return 1
    for song_dir, song_id in songs:
        print(f"[{song_id}] 拍号分析")
        process_song(song_dir, [args.difficulty] if args.difficulty is not None else None, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
