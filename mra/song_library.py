"""
歌曲库发现与目录整理
=====================
自动扫描歌曲目录，将含 maidata.txt 的文件夹整理到统一的 songs/ 库中。
提供 find_song_dirs() 和 discover_song_folders() 两个接口，
供其他模块 (visualize.py / make_html.py / etc.) 调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .simai_parser import NoteType, parse_maidata


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SONGS_DIR_NAME = "songs"  # 默认歌曲库目录名

# Windows 文件名不允许的字符 → 全角替换
_WINDOWS_REPLACEMENTS = str.maketrans({
    '<': '＜', '>': '＞', ':': '：', '"': '＂', '/': '／',
    '\\': '＼', '|': '｜', '?': '？', '*': '＊',
})
# Windows 保留设备名 (不能作为文件/文件夹名)
_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


@dataclass(frozen=True)
class SongFolder:
    """歌曲目录信息"""
    path: Path               # 目录路径
    title: str               # 曲名 (从 maidata.txt 读取)
    previous_name: str       # 重命名前的目录名


def safe_folder_name(title: str, fallback: str = "untitled") -> str:
    """
    将曲名转为 Windows 安全的文件夹名。
    - 移除控制字符
    - 特殊符号转为全角
    - 避免保留设备名
    """
    name = re.sub(r'[\x00-\x1f]', '', (title or '').strip())
    name = name.translate(_WINDOWS_REPLACEMENTS).rstrip(' .')
    if not name:
        name = fallback
    if name.split('.', 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        name = f'_{name}'
    return name[:180].rstrip(' .') or fallback


def _is_dx_chart(song) -> bool:
    """
    按谱面内容判断是否为 DX 曲目。
    规则:
    - 只要任一难度出现 touch / touch hold / 烟花触摸(保护套 tap) 即视为 DX
    """
    for chart in song.charts.values():
        for note in chart.notes:
            if note.note_type in {NoteType.TOUCH, NoteType.TOUCH_HOLD}:
                return True
            if note.is_firework:
                return True
    return False


def _song_title(song_dir: Path) -> str:
    """从 maidata.txt 读取曲名，并按谱面特征补 DX 后缀。"""
    song = parse_maidata(str(song_dir / 'maidata.txt'))
    title = song.title.strip() or song_dir.name
    if _is_dx_chart(song) and not title.endswith('[DX]'):
        title = f'{title} [DX]'
    return title


def _is_song_dir(path: Path) -> bool:
    """判断目录是否包含 maidata.txt"""
    return path.is_dir() and (path / 'maidata.txt').is_file()


def _unique_destination(library_dir: Path, desired_name: str,
                        source_name: str) -> Path:
    """
    处理重名冲突: 如果目标文件夹已存在，自动加后缀 [原名] 或 [原名 N]。
    """
    destination = library_dir / desired_name
    if not destination.exists():
        return destination

    collision_prefix = f'{desired_name} ['
    if source_name.startswith(collision_prefix) and source_name.endswith(']'):
        return library_dir / source_name

    suffix = safe_folder_name(source_name, 'duplicate')
    destination = library_dir / f'{desired_name} [{suffix}]'
    if not destination.exists():
        return destination

    index = 2
    while (library_dir / f'{desired_name} [{suffix} {index}]').exists():
        index += 1
    return library_dir / f'{desired_name} [{suffix} {index}]'


def _organize_song(source: Path, library_dir: Path) -> SongFolder:
    """
    整理单首歌曲: 如果目录名不同于安全化的曲名，自动重命名并移到 songs/ 库。
    """
    source = source.resolve()
    library_dir = library_dir.resolve()
    previous_name = source.name
    title = _song_title(source)
    desired_name = safe_folder_name(title, previous_name)

    # 已在正确位置且名称正确 → 无需操作
    if source.parent == library_dir and source.name == desired_name:
        return SongFolder(source, title, previous_name)

    if (source.parent == library_dir and source.name.startswith(f'{desired_name} [')
            and source.name.endswith(']') and (library_dir / desired_name).exists()):
        return SongFolder(source, title, previous_name)

    destination = _unique_destination(library_dir, desired_name, previous_name)
    if destination != source:
        source.rename(destination)
        print(f'  整理歌曲目录: {previous_name} -> {destination.name}')
    return SongFolder(destination, title, previous_name)


def discover_song_folders(base_dir: str | Path, selector: str | None = None,
                          project_root: str | Path | None = None) -> list[SongFolder]:
    """Discover songs and organize the default project library under ``songs/``."""
    base = Path(base_dir).resolve()
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    if not base.is_dir():
        return []

    if _is_song_dir(base):
        library_dir = base.parent
        candidates = [base]
    elif base == root:
        library_dir = root / SONGS_DIR_NAME
        library_dir.mkdir(exist_ok=True)
        legacy = [entry for entry in base.iterdir()
                  if entry != library_dir and _is_song_dir(entry)]
        current = [entry for entry in library_dir.iterdir() if _is_song_dir(entry)]
        candidates = legacy + current
    else:
        nested_library = base / SONGS_DIR_NAME
        library_dir = nested_library if nested_library.is_dir() else base
        candidates = [entry for entry in library_dir.iterdir() if _is_song_dir(entry)]

    organized = []
    for candidate in sorted(candidates, key=lambda path: path.name.casefold()):
        organized.append(_organize_song(candidate, library_dir))

    if selector:
        wanted = selector.casefold()
        directory_matches = [
            song for song in organized
            if wanted in {
                song.path.name.casefold(),
                song.previous_name.casefold(),
                str(song.path).casefold(),
            }
        ]
        if directory_matches:
            organized = directory_matches
        else:
            organized = [
                song for song in organized
                if wanted == song.title.casefold()
            ]
    return sorted(organized, key=lambda song: song.path.name.casefold())


def find_song_dirs(base_dir: str | Path, selector: str | None = None,
                   project_root: str | Path | None = None) -> list[tuple[str, str]]:
    """Return the tuple shape used by the existing processing scripts."""
    return [(str(song.path), song.path.name) for song in discover_song_folders(
        base_dir, selector=selector, project_root=project_root
    )]
