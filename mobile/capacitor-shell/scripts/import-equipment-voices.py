#!/usr/bin/env python3
"""Нарезает утверждённую озвучку номеров техники в Android raw-ресурсы."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path


TRUCK_NUMBERS = [
    *[str(number) for number in range(10, 53)],
    *[str(number) for number in range(54, 64)],
    "test_1",
]
EXCAVATOR_ASSIGNMENTS = [
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "99", "528", "530", "tvi_4",
]
EXCAVATOR_RESERVE_ASSIGNMENTS = ["2_reserve", "test_e99"]
DUMP_POINT_SUFFIXES = [
    "bufernyi_sklad",
    "kkd",
    "otval",
    "podsypka",
    "svh",
    "skdr",
    "sklad_negabarita",
    "sklad_okislennoy_rudy",
]
COMMON_SUFFIXES = [
    "voice_truck_assigned_prefix",
    "voice_truck_removed_prefix",
    "voice_truck_number_prefix",
]


@dataclass(frozen=True)
class Batch:
    key: str
    source: Path
    output_names: list[str]
    profile: str
    threshold_db: float
    minimum_silence_seconds: float


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_pcm(ffmpeg: Path, source: Path, target: Path) -> None:
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
        ],
        check=True,
    )


def silence_gaps(
    pcm_path: Path,
    threshold_db: float,
    minimum_seconds: float,
) -> tuple[float, list[tuple[float, float]]]:
    with wave.open(str(pcm_path), "rb") as audio:
        sample_rate = audio.getframerate()
        samples = array.array("h", audio.readframes(audio.getnframes()))
    duration = len(samples) / sample_rate
    frame_samples = max(1, round(sample_rate * 0.01))
    levels = []
    for start in range(0, len(samples), frame_samples):
        frame = samples[start:start + frame_samples]
        rms = math.sqrt(sum(value * value for value in frame) / max(1, len(frame)))
        levels.append(20 * math.log10(max(rms, 1) / 32768))

    gaps: list[tuple[float, float]] = []
    start_index: int | None = None
    for index, level in enumerate([*levels, 0.0]):
        if level < threshold_db and start_index is None:
            start_index = index
        elif level >= threshold_db and start_index is not None:
            gap_start = start_index * 0.01
            gap_end = index * 0.01
            if gap_end - gap_start >= minimum_seconds:
                gaps.append((gap_start, gap_end))
            start_index = None
    # Конечная тишина не разделяет реплики.
    gaps = [gap for gap in gaps if gap[1] < duration - 0.02]
    return duration, gaps


def encode_segment(
    ffmpeg: Path,
    source: Path,
    target: Path,
    start: float,
    end: float,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    segment_duration = end - start
    fade_out_start = max(0.0, segment_duration - 0.015)
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(source),
            "-t", f"{segment_duration:.3f}",
            "-af", f"aresample=48000,afade=t=in:st=0:d=0.015,afade=t=out:st={fade_out_start:.3f}:d=0.015",
            "-ac", "1", "-ar", "48000", "-c:a", "aac", "-b:a", "96k",
            "-map_metadata", "-1", "-movflags", "+faststart", str(target),
        ],
        check=True,
    )


def verify_encoded_segment(ffmpeg: Path, target: Path, expected_duration: float) -> float:
    result = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error",
            "-i", str(target), "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    samples = array.array("h")
    samples.frombytes(result.stdout)
    actual_duration = len(samples) / 16000
    if not samples or abs(actual_duration - expected_duration) > 0.08:
        raise RuntimeError(
            f"{target.name}: длительность {actual_duration:.3f}, ожидалось {expected_duration:.3f}"
        )
    peak = max(abs(value) for value in samples)
    peak_db = 20 * math.log10(max(peak, 1) / 32768)
    if peak_db < -20.0:
        raise RuntimeError(f"{target.name}: вместо речи записана тишина ({peak_db:.1f} dBFS)")
    return peak_db


def import_batch(ffmpeg: Path, project_root: Path, batch: Batch) -> dict:
    with tempfile.TemporaryDirectory(prefix="equipment-voice-") as temp_dir:
        pcm_path = Path(temp_dir) / "source.wav"
        decode_pcm(ffmpeg, batch.source, pcm_path)
        duration, gaps = silence_gaps(
            pcm_path,
            batch.threshold_db,
            batch.minimum_silence_seconds,
        )
    expected_gaps = len(batch.output_names) - 1
    # В записи «тест один» есть естественная пауза между двумя словами. Она
    # короче обычного разделителя, но пересекает порог детектора; это не новая
    # реплика, поэтому последний лишний разрыв объединяем обратно.
    if batch.key == "truckNumbers" and len(gaps) == expected_gaps + 1:
        gaps.pop()
    if len(gaps) != expected_gaps:
        raise RuntimeError(
            f"{batch.source.name}: найдено {len(gaps)} разделителей, ожидалось {expected_gaps}"
        )
    boundaries = [0.0, *[(start + end) / 2 for start, end in gaps], duration]
    segments = []
    raw_root = project_root / "profiles" / batch.profile / "res" / "raw"
    for index, output_name in enumerate(batch.output_names):
        target = raw_root / f"{batch.profile}_{output_name}.m4a"
        encode_segment(ffmpeg, batch.source, target, boundaries[index], boundaries[index + 1])
        peak_db = verify_encoded_segment(
            ffmpeg,
            target,
            boundaries[index + 1] - boundaries[index],
        )
        segments.append({
            "index": index + 1,
            "resource": target.name,
            "startSeconds": round(boundaries[index], 3),
            "endSeconds": round(boundaries[index + 1], 3),
            "peakDbfs": round(peak_db, 1),
            "sha256": sha256(target),
        })
    return {
        "source": batch.source.name,
        "sourceSha256": sha256(batch.source),
        "profile": batch.profile,
        "segments": segments,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    args = parser.parse_args()
    ffmpeg = args.ffmpeg.resolve()
    source_dir = args.source_dir.resolve()
    project_root = Path(__file__).resolve().parents[1]
    batches = [
        Batch(
            "excavatorCommon",
            source_dir / "1. Общие фразы экскаваторщика.mp3",
            COMMON_SUFFIXES,
            "excavator",
            -25.0,
            0.24,
        ),
        Batch(
            "excavatorDestinations",
            source_dir / "2. Подтверждение отправки на разгрузку.mp3",
            [f"voice_truck_sent_{suffix}" for suffix in DUMP_POINT_SUFFIXES],
            "excavator",
            -24.0,
            0.24,
        ),
        Batch(
            "truckNumbers",
            source_dir / "3. Номера самосвалов.mp3",
            [f"voice_truck_number_{number}" for number in TRUCK_NUMBERS],
            "excavator",
            -25.0,
            0.24,
        ),
        Batch(
            "driverAssignments",
            source_dir / "4. Назначения для водителей.mp3",
            [f"voice_excavator_assignment_{number}" for number in EXCAVATOR_ASSIGNMENTS],
            "driver",
            -24.0,
            0.32,
        ),
        Batch(
            "driverReserveAssignments",
            source_dir / "5. Дополнительно для резерва и тестирования.mp3",
            [f"voice_excavator_assignment_{number}" for number in EXCAVATOR_RESERVE_ASSIGNMENTS],
            "driver",
            -25.0,
            0.50,
        ),
    ]
    for batch in batches:
        if not batch.source.is_file():
            raise FileNotFoundError(batch.source)
    manifest = {
        "schemaVersion": 1,
        "description": "Записанная пользователем озвучка номеров техники и подтверждений назначения.",
        "format": {"container": "M4A", "codec": "AAC", "sampleRateHz": 48000, "channels": 1},
        "batches": {
            batch.key: import_batch(ffmpeg, project_root, batch)
            for batch in batches
        },
    }
    manifest_path = project_root / "audio" / "equipment-voices-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
