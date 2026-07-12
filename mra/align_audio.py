#!/usr/bin/env python3
"""
音频自动对齐 v4 — 原曲与预览音轨相关匹配
========================================
优先对 track.mp3 与谱面预览视频的整段音轨做能量包络相关匹配，再用波形细化。
没有原曲或匹配置信度不足时，才回退到首个持续高能量 onset 检测。

用法:
  python align_audio.py -d "WiPE OUT MEMORIES"         # 自动对齐 MASTER
  python align_audio.py -d "WiPE OUT MEMORIES" -diff 4 # 指定难度
"""
import os, sys, argparse, subprocess, tempfile, wave
import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

from .simai_parser import parse_maidata
from .visualize import compute_rhythm_events
from .difficulty import (DIFFICULTY_NAMES, default_target_difficulties,
                         find_preview_video, offset_file_path, preview_video_candidates)
from .song_library import PROJECT_ROOT, find_song_dirs

ALIGN_SAMPLE_RATE = 22050  # 提高采样率减少量化误差 (原 16000)


def extract_audio_mono(path, sr=22050, duration=None):
    """
    使用 FFmpeg 从视频/音频文件提取单声道 WAV。
    返回 (float32归一化波形, 采样率)。
    """
    tmp = tempfile.mktemp(suffix='.wav')
    cmd = ['ffmpeg', '-y', '-i', path, '-vn', '-ac', '1', '-ar', str(sr), '-f', 'wav']
    if duration:
        cmd += ['-t', str(duration)]
    cmd.append(tmp)
    subprocess.run(cmd, capture_output=True)
    if not os.path.exists(tmp):
        return None, sr
    with wave.open(tmp, 'rb') as w:
        frames = w.readframes(w.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    os.remove(tmp)
    return audio, sr


def detect_first_onset(audio, sr, min_duration=0.3):
    """
    检测首个持续高能量的 onset (tap 音效到达判定线)。
    用于回退方案: 整曲匹配失败时，用能量检测找到第一个明显的 tap 音效。
    min_duration: 高能量需持续多久才判定为真正的 onset (过滤短暂噪声)。
    """
    if audio is None or len(audio) < sr:
        return None

    frame_size = int(sr * 0.005)  # 5ms 帧 (原 10ms)，更精细的时间粒度
    hop = int(sr * 0.0025)        # 2.5ms 跳跃 (原 5ms)
    energy = []
    for i in range(0, len(audio) - frame_size, hop):
        e = np.sqrt(np.mean(audio[i:i+frame_size]**2))
        energy.append(e)
    energy = np.array(energy)
    energy_smooth = uniform_filter1d(energy, size=5)

    # 自适应阈值: 用全曲能量的百分位
    threshold = np.percentile(energy_smooth[energy_smooth > 1e-6], 40) * 0.3  # P40 (原 P50)，更低阈值更敏感
    if threshold < 0.01:
        threshold = 0.01

    # 找首个"持续高能量"段
    min_frames = int(min_duration / 0.0025)
    start_frame = int(1.0 / 0.0025)  # 跳过前 1 秒 (原 2s)，捕获更早的 onset

    for i in range(start_frame, len(energy_smooth) - min_frames):
        if energy_smooth[i] > threshold:
            segment = energy_smooth[i:i + min_frames]
            if np.mean(segment > threshold * 0.5) > 0.6:
                onset_time = i * hop / sr
                return onset_time
    return None


def _energy_envelope(audio, sr, frame_seconds=0.008):  # 8ms (原 20ms)，包络时间分辨率提升 2.5×
    """
    提取音频的能量包络 (用于互相关匹配)。
    返回 (标准化包络, 削波后波形, 帧大小)。
    """
    frame = max(1, int(sr * frame_seconds))
    centered = np.clip(audio - np.mean(audio), -0.50, 0.50)  # 放宽削波范围 (原 ±0.35)，保留更多动态
    energy = np.sqrt(uniform_filter1d(centered * centered, size=frame, mode='nearest'))
    envelope = energy[::frame]
    return (envelope - np.mean(envelope)) / (np.std(envelope) + 1e-9), centered, frame


def _aligned_correlation(video, track, lag):
    """
    计算两个音频信号在指定滞后(lag)下的归一化互相关。
    用于衡量匹配置信度。
    """
    if lag >= 0:
        size = min(len(video) - lag, len(track))
        left, right = video[lag:lag + size], track[:size]
    else:
        size = min(len(video), len(track) + lag)
        left, right = video[:size], track[-lag:-lag + size]
    if size < 2:
        return 0.0
    return float(np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right) + 1e-9))


def estimate_track_offset(video_audio, track_audio, sr, min_lag=-15.0, max_lag=45.0):  # 收紧搜索范围 (原 -30~60s)，减少假峰值
    """Return (video_time - track_time, confidence) using full-song correlation."""
    if video_audio is None or track_audio is None:
        return None, 0.0
    if min(len(video_audio), len(track_audio)) < sr * 10:
        return None, 0.0

    video_env, video_wave, hop = _energy_envelope(video_audio, sr)
    track_env, track_wave, _ = _energy_envelope(track_audio, sr)
    correlations = signal.correlate(video_env, track_env, mode='full', method='fft')
    lags = signal.correlation_lags(len(video_env), len(track_env), mode='full')
    envelope_rate = sr / hop
    valid = (lags >= min_lag * envelope_rate) & (lags <= max_lag * envelope_rate)
    if not np.any(valid):
        return None, 0.0
    valid_indices = np.flatnonzero(valid)
    best_index = valid_indices[np.argmax(correlations[valid])]
    coarse_lag = int(lags[best_index])
    confidence = _aligned_correlation(video_env, track_env, coarse_lag)

    # Refine the 20ms envelope result against a long waveform section.
    coarse_samples = int(round(coarse_lag * hop))
    radius = max(1, int(sr * 0.15))  # 搜索半径 150ms (原 80ms)，允许更大粗对齐误差
    margin = int(sr * 5)  # 首尾各跳 5s (原 10s)，用更多数据细化
    track_start = max(0, -coarse_samples) + margin
    track_end = min(len(track_wave), len(video_wave) - coarse_samples) - margin
    if track_end - track_start >= sr * 20:
        # 不限制细化片段长度 (原限 90s)，全量数据提高信噪比
        video_start = track_start + coarse_samples - radius
        video_end = track_end + coarse_samples + radius
        if video_start >= 0 and video_end <= len(video_wave):
            waveform_corr = signal.correlate(
                video_wave[video_start:video_end],
                track_wave[track_start:track_end],
                mode='valid', method='fft',
            )
            coarse_samples += int(np.argmax(waveform_corr)) - radius

    return coarse_samples / sr, confidence


def align_song(song_dir, song_id, diff_id=5, force=False):
    diff_name = DIFFICULTY_NAMES.get(diff_id, diff_id)
    video_name = find_preview_video(song_dir, diff_id)
    video_path = os.path.join(song_dir, video_name) if video_name else None
    maidata = os.path.join(song_dir, 'maidata.txt')
    track_path = os.path.join(song_dir, 'track.mp3')
    offset_file = offset_file_path(song_dir, diff_id)
    offset_file.parent.mkdir(parents=True, exist_ok=True)

    if not video_path:
        print(f'  [{song_id}] 无 {preview_video_candidates(diff_id)[0]}'); return None
    if not os.path.exists(maidata):
        print(f'  [{song_id}] 无 maidata.txt'); return None

    # 谱面首个音符时间 (相对谱面开始)
    song = parse_maidata(maidata)
    if diff_id not in song.charts:
        print(f'  [{song_id}] 无难度 {diff_name}'); return None
    ch = song.charts[diff_id]
    ev = compute_rhythm_events(ch)
    if not ev:
        print(f'  [{song_id}] 无音符'); return None
    first_note_time = ev[0]['time']  # 相对谱面开始
    first_offset = song.first_offset
    # 谱面首音符的绝对时间 (相对音源开头)
    first_note_abs = first_offset + first_note_time

    # 提高采样率，降低对齐时间量化误差；16000Hz 仍能控制整曲 FFT 的内存与耗时。
    print(f'  [{song_id}] 提取预览音轨...', end=' ', flush=True)
    v_audio, sr = extract_audio_mono(video_path, sr=ALIGN_SAMPLE_RATE)
    if v_audio is None:
        print('失败'); return None
    print(f'✓ {len(v_audio)/sr:.1f}s')

    offset = None
    if os.path.exists(track_path):
        print(f'  [{song_id}] 提取原曲音轨...', end=' ', flush=True)
        track_audio, _ = extract_audio_mono(track_path, sr=sr)
        if track_audio is None:
            print('失败')
        else:
            print(f'✓ {len(track_audio)/sr:.1f}s')
            media_offset, confidence = estimate_track_offset(v_audio, track_audio, sr)
            if media_offset is not None:
                print(f'  [{song_id}] 整曲音频匹配: {media_offset:+.4f}s, 置信度 {confidence:.3f}')
                if confidence >= 0.15:  # 放宽置信度阈值 (原 0.20)，减少误拒绝
                    first_note_video_time = media_offset + first_note_abs
                    offset = first_note_video_time - first_note_time
                else:
                    print(f'  [{song_id}] 匹配置信度不足, 回退到首音检测')

    if offset is None:
        first_tap = detect_first_onset(v_audio, sr, min_duration=0.2)  # 更短的持续判定 (原 0.3s)
        if first_tap is None:
            print(f'  [{song_id}] 未检测到可用对齐点'); return None
        offset = first_tap - first_note_time
        print(f'  [{song_id}] 首音回退: {first_tap:.3f}s, 谱面首音符: {first_note_abs:.3f}s')

    first_note_video_time = offset + first_note_time
    first_note_scroll_time = offset + first_note_time
    print(f'  [{song_id}] 视频首音到达判定线: {first_note_video_time:.4f}s')
    print(f'  [{song_id}] 滚动条首音经过圆圈: {first_note_scroll_time:.4f}s')
    print(f'  [{song_id}] VIDEO_OFFSET = {offset:+.3f}s')

    with open(offset_file, 'w', encoding='utf-8') as f:
        f.write(f'{offset:.4f}\n')
    print(f'  [{song_id}] 已保存 {offset_file.relative_to(song_dir)}')
    return offset


def main():
    ap = argparse.ArgumentParser(description='音频自动对齐 (整曲相关匹配)')
    ap.add_argument('-i', '--input', default=None, help='歌曲根目录')
    ap.add_argument('-d', '--dir', default=None, help='只处理指定曲目名')
    ap.add_argument('-diff', '--difficulty', type=int, default=None,
                    help='难度 ID；不指定则默认只处理 MASTER/Re:MASTER')
    ap.add_argument('-f', '--force', action='store_true',
                    help='兼容参数；音频偏移现在每次都会重新计算')
    args = ap.parse_args()

    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    if not os.path.isdir(base_dir):
        print(f'错误: {base_dir} 不存在'); sys.exit(1)

    songs = find_song_dirs(base_dir, args.dir)
    if not songs:
        print(f'在 {base_dir} 下未找到含 maidata.txt 的目录'); return

    difficulty_label = (DIFFICULTY_NAMES.get(args.difficulty, args.difficulty)
                        if args.difficulty is not None else '默认 MASTER/Re:MASTER')
    print(f'发现 {len(songs)} 首歌曲, {difficulty_label}\n')
    failures = 0
    for sd, sid in songs:
        available_difficulties = [did for did in range(1, 8) if find_preview_video(sd, did)]
        difficulties = ([args.difficulty] if args.difficulty is not None else
                        default_target_difficulties(available_difficulties))
        if not difficulties:
            print(f'  [{sid}] 未发现谱面预览视频, 跳过')
            continue
        for difficulty in difficulties:
            try:
                result = align_song(sd, sid, difficulty, args.force)
                if result is None:
                    failures += 1
            except Exception as e:
                print(f'  [{sid}] {DIFFICULTY_NAMES.get(difficulty, difficulty)} ✗ {e}')
                failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
