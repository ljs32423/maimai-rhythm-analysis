"""
难度标签定义与文件系统安全名称
================================
提供 maimai 谱面难度编号到可读名称的映射，以及
文件名安全化处理（尤其是 Re:MASTER 的多种别名）。
其他模块统一通过此模块获取难度名称，避免硬编码。
"""

from pathlib import Path

# --- 难度编号 → 显示名称 ---
# 编号来自 simai 格式的 &lv_N= 字段，N 的取值范围 1-7
DIFFICULTY_NAMES = {
    1: "EASY",
    2: "BASIC",
    3: "ADVANCED",
    4: "EXPERT",
    5: "MASTER",
    6: "Re:MASTER",
    7: "UTOPIA",
}

# --- 难度编号 → 文件名安全前缀 ---
# Re:MASTER 在 Windows 文件名中不能用冒号(:)，所以单独处理为 "ReMASTER"
DIFFICULTY_FILE_STEMS = {
    **DIFFICULTY_NAMES,
    6: "ReMASTER",
}
DEFAULT_ANALYSIS_DIFFICULTIES = (5, 6)
OUTPUTS_DIR = "outputs"


def difficulty_name(difficulty_id: int) -> str:
    """返回难度编号的可读名称（如 5 → "MASTER"）"""
    return DIFFICULTY_NAMES.get(difficulty_id, f"D{difficulty_id}")


def difficulty_file_stem(difficulty_id: int) -> str:
    """返回难度编号的文件名安全前缀（如 6 → "ReMASTER"，不含冒号）"""
    return DIFFICULTY_FILE_STEMS.get(difficulty_id, f"D{difficulty_id}")


def difficulty_output_dir(song_dir: str | Path, difficulty_id: int) -> Path:
    """返回该难度的产物根目录：outputs/<难度>/"""
    return Path(song_dir) / OUTPUTS_DIR / difficulty_file_stem(difficulty_id)


def difficulty_output_path(song_dir: str | Path, difficulty_id: int,
                           category: str, filename: str) -> Path:
    """返回分类后的难度产物路径。"""
    return difficulty_output_dir(song_dir, difficulty_id) / category / filename


def analysis_html_path(song_dir: str | Path, difficulty_id: int) -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "html", "analysis.html")


def preview_video_path(song_dir: str | Path, difficulty_id: int, extension: str = ".mp4") -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "video", f"preview{extension}")


def offset_file_path(song_dir: str | Path, difficulty_id: int) -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "sync", "offset.txt")


def meter_file_path(song_dir: str | Path, difficulty_id: int) -> Path:
    """返回人工维护的拍号变化文件路径。"""
    return difficulty_output_path(song_dir, difficulty_id, "meter", "meter.json")


def rhythm_svg_path(song_dir: str | Path, difficulty_id: int) -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "rhythm", "rhythm.svg")


def rhythm_png_path(song_dir: str | Path, difficulty_id: int) -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "rhythm", "rhythm.png")


def strip_svg_path(song_dir: str | Path, difficulty_id: int) -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "strip", "strip.svg")


def strip_segment_base_path(song_dir: str | Path, difficulty_id: int) -> Path:
    return difficulty_output_path(song_dir, difficulty_id, "strip", "segments") / "strip"


def legacy_difficulty_path(song_dir: str | Path, difficulty_id: int, suffix: str) -> Path:
    """返回旧版扁平产物路径，用于兼容和迁移。suffix 形如 '_preview.mp4'。"""
    return Path(song_dir) / f"{difficulty_file_stem(difficulty_id)}{suffix}"


def relative_asset_path(song_dir: str | Path, target: str | Path) -> str:
    """返回 target 相对歌曲目录的 POSIX 路径。"""
    return Path(target).relative_to(Path(song_dir)).as_posix()


def difficulty_file_aliases(difficulty_id: int) -> list[str]:
    """
    返回难度的所有可能文件名前缀。
    Re:MASTER (difficulty_id=6) 有 6 种常见写法：
    "ReMASTER", "REMASTER", "Re-MASTER", "Re_MASTER", "Re MASTER", "Re：MASTER"
    其他难度只返回标准前缀。
    """
    stem = difficulty_file_stem(difficulty_id)
    if difficulty_id != 6:
        return [stem]
    return [stem, "REMASTER", "Re-MASTER", "Re_MASTER", "Re MASTER", "Re：MASTER"]


def default_target_difficulties(existing_difficulties) -> list[int]:
    existing = sorted({int(difficulty) for difficulty in existing_difficulties})
    preferred = [difficulty for difficulty in DEFAULT_ANALYSIS_DIFFICULTIES if difficulty in existing]
    return preferred or existing


def preview_video_candidates(difficulty_id: int) -> list[str]:
    """
    返回该难度的预览视频候选文件名列表。
    支持 .mp4 / .webm / .mkv 三种扩展名，覆盖所有别名。
    """
    extensions = (".mp4", ".webm", ".mkv")
    new_candidates = [
        (Path(OUTPUTS_DIR) / difficulty_file_stem(difficulty_id) / "video" /
         f"preview{extension}").as_posix()
        for extension in extensions
    ]
    legacy_candidates = [
        f"{alias}_preview{extension}"
        for alias in difficulty_file_aliases(difficulty_id)
        for extension in extensions
    ]
    return new_candidates + legacy_candidates


def _normalized_asset_name(name: str) -> str:
    """
    将文件名归一化：只保留字母数字字符并转为小写。
    用于宽松匹配：忽略空格、标点、全角符号差异。
    """
    return "".join(char.casefold() for char in name if char.isalnum())


def find_preview_video(song_dir: str, difficulty_id: int) -> str | None:
    """
    在歌曲目录中查找预览视频文件的实际文件名。
    先精确匹配候选名，再通过归一化(只保留字母数字)宽松匹配。
    返回实际文件名（含扩展名），未找到时返回 None。
    """
    directory = Path(song_dir)
    if not directory.is_dir():
        return None

    # 第一轮：新目录结构精确匹配
    candidates = preview_video_candidates(difficulty_id)
    for candidate in candidates:
        path = directory / candidate
        if path.is_file():
            return path.relative_to(directory).as_posix()

    # 第二轮：旧目录结构精确匹配（大小写不敏感）
    legacy_candidates = [Path(candidate).name for candidate in candidates if "/" not in candidate]
    files = {path.name.casefold(): path.name for path in directory.iterdir() if path.is_file()}
    for candidate in legacy_candidates:
        match = files.get(candidate.casefold())
        if match:
            return match

    # 第三轮：旧目录结构宽松匹配（忽略所有非字母数字字符）
    normalized = {_normalized_asset_name(name) for name in legacy_candidates}
    for path in directory.iterdir():
        if path.is_file() and _normalized_asset_name(path.name) in normalized:
            return path.name
    return None
