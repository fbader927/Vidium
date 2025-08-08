import os
import sys
import asyncio
import json
import shutil
from datetime import datetime
from typing import Dict, Optional, Tuple, List

from converter import (
    get_ffmpeg_path,
    get_ffprobe_path,
    run_ffmpeg,
    convert_file,
    OUTPUT_FOLDER,
)


TEST_OUTPUT_SUBDIR = os.path.join(OUTPUT_FOLDER, "test_matrix")
RESULTS_LOG_PATH = os.path.join(TEST_OUTPUT_SUBDIR, "results.txt")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def human_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} TB"


def probe_media(file_path: str) -> Dict:
    ffprobe = get_ffprobe_path()
    args = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        file_path,
    ]
    import subprocess
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {file_path}: {result.stderr}")
    return json.loads(result.stdout or "{}")


def extract_video_audio_meta(ffprobe_json: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    streams = ffprobe_json.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return v, a


async def convert_from_to(input_path: str, target_ext: str, use_gpu: bool = False) -> str:
    ensure_dir(TEST_OUTPUT_SUBDIR)
    base = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(TEST_OUTPUT_SUBDIR, f"{base}.{target_ext}")

    target_ext_lower = target_ext.lower()
    extra_args: Optional[List[str]] = None

    if target_ext_lower in {"mp4", "mkv"}:
        extra_args = [
            "-pix_fmt", "yuv420p",
            "-r", "60",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
        ]
    elif target_ext_lower == "webm":
        # Match GUI VP9 settings
        extra_args = [
            "-pix_fmt",
            "yuv420p",
            "-r",
            "60",
            "-c:v",
            "libvpx-vp9",
            "-quality",
            "good",
            "-cpu-used",
            "4",
            "-tile-columns",
            "6",
            "-frame-parallel",
            "1",
            "-crf",
            "30",
            "-b:v",
            "0",
        ]
        use_gpu = False  # enforce CPU for VP9
    elif target_ext_lower == "gif":
        # Palette-based GIF (two-pass)
        palette_path = os.path.join(TEST_OUTPUT_SUBDIR, f"{base}_palette.png")
        try:
            palette_args = [
                "-y",
                "-i",
                input_path,
                "-vf",
                "fps=12,scale=320:-1:flags=lanczos,palettegen",
                "-frames:v",
                "1",
                palette_path,
            ]
            ret = await run_ffmpeg(palette_args)
            if ret != 0:
                raise RuntimeError("Palette generation failed")
            gif_args = [
                "-y",
                "-i",
                input_path,
                "-i",
                palette_path,
                "-filter_complex",
                "fps=12,scale=320:-1:flags=lanczos[x];[x][1:v]paletteuse",
                output_path,
            ]
            ret2 = await run_ffmpeg(gif_args)
            if ret2 != 0:
                raise RuntimeError("GIF encode failed")
            return output_path
        finally:
            if os.path.exists(palette_path):
                try:
                    os.remove(palette_path)
                except Exception:
                    pass
    elif target_ext_lower == "mp3":
        extra_args = ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
    elif target_ext_lower == "wav":
        extra_args = ["-vn", "-c:a", "pcm_s16le", "-ar", "48000"]
    else:
        raise ValueError(f"Unsupported target extension: {target_ext}")

    # Try GPU if requested and supported, else fall back to CPU once
    try:
        await convert_file(input_path, output_path, extra_args=extra_args, use_gpu=use_gpu)
    except Exception:
        if use_gpu and target_ext_lower in {"mp4", "mkv"}:
            # GPU path failed (e.g., no CUDA/NVDEC). Retry on CPU.
            await convert_file(input_path, output_path, extra_args=extra_args, use_gpu=False)
        else:
            raise
    return output_path


def size_ratio_bounds_for_target(target_ext: str) -> Tuple[float, float]:
    t = target_ext.lower()
    if t == "webm":
        return (0.20, 2.50)
    if t in {"mp4", "mkv"}:
        return (0.40, 2.50)
    # GIF and audio sizes vary greatly; skip strict checks
    return (0.0, float("inf"))


def roughly_equal_duration(d1: Optional[float], d2: Optional[float], tolerance_s: float = 0.75) -> bool:
    if d1 is None or d2 is None:
        return True
    return abs(d1 - d2) <= tolerance_s


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Vidium conversion matrix test")
    parser.add_argument("--base", default="michael.mp4", help="Path to base video file (default: michael.mp4)")
    # Default to GPU unless explicitly disabled. CPU fallback will be attempted automatically on failure.
    parser.add_argument("--no-gpu", action="store_true", help="Disable GPU usage even where supported")
    parser.add_argument("--rebuild-sources", action="store_true", help="Rebuild derived source formats from base before running matrix")
    args = parser.parse_args()

    base_video = os.path.abspath(args.base)
    if not os.path.exists(base_video):
        print(f"Base file not found: {base_video}")
        sys.exit(1)

    ensure_dir(TEST_OUTPUT_SUBDIR)

    # Build initial set of source files
    sources: Dict[str, str] = {"mp4": base_video}
    derived_targets = ["mkv", "webm", "gif", "mp3", "wav"]

    if args.rebuild_sources:
        # Clean previous derived sources
        for ext in derived_targets:
            p = os.path.join(TEST_OUTPUT_SUBDIR, f"michael.{ext}")
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # Ensure derived sources exist (for matrix inputs)
    default_use_gpu = not args.no_gpu
    for ext in derived_targets:
        outp = os.path.join(TEST_OUTPUT_SUBDIR, f"michael.{ext}")
        if not os.path.exists(outp):
            try:
                print(f"[build] Converting base -> {ext}")
                await convert_from_to(base_video, ext, use_gpu=default_use_gpu)
            except Exception as e:
                print(f"Failed to build source {ext}: {e}")
                if os.path.exists(outp):
                    try:
                        os.remove(outp)
                    except Exception:
                        pass
                # Non-fatal for GIF/audio; continue
        if os.path.exists(outp):
            sources[ext] = outp

    # Define conversion matrix (only meaningful conversions)
    video_exts = [e for e in ["mp4", "mkv", "webm"] if e in sources]
    audio_exts = [e for e in ["mp3", "wav"] if e in sources]

    matrix: List[Tuple[str, str]] = []
    # video -> video
    for src in video_exts:
        for dst in video_exts:
            if src == dst:
                continue
            matrix.append((src, dst))
    # video -> audio
    for src in video_exts:
        for dst in audio_exts:
            matrix.append((src, dst))
    # audio -> audio
    for src in audio_exts:
        for dst in audio_exts:
            if src == dst:
                continue
            matrix.append((src, dst))
    # include video -> gif basic functional check
    if "gif" in sources:
        for src in video_exts:
            matrix.append((src, "gif"))

    print(f"Planned conversions: {len(matrix)}")

    # Prepare results logging
    report_lines: List[str] = []
    ensure_dir(TEST_OUTPUT_SUBDIR)
    header = f"===== Test Run {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | GPU default: {'ON' if default_use_gpu else 'OFF'} ====="
    report_lines.append(header)

    failures: List[str] = []

    # Probe base video metadata for comparisons
    base_meta = probe_media(sources["mp4"]) if "mp4" in sources else None
    base_v_stream, _ = extract_video_audio_meta(base_meta) if base_meta else (None, None)
    base_w = base_v_stream.get("width") if base_v_stream else None
    base_h = base_v_stream.get("height") if base_v_stream else None

    for (src_ext, dst_ext) in matrix:
        src_file = sources[src_ext]
        dst_file = os.path.join(
            TEST_OUTPUT_SUBDIR, f"{os.path.splitext(os.path.basename(src_file))[0]}_to_{dst_ext}.{dst_ext}"
        )
        if os.path.exists(dst_file):
            try:
                os.remove(dst_file)
            except Exception:
                pass
        print(f"[test] {src_ext} -> {dst_ext}")
        try:
            # Perform conversion
            await convert_from_to(src_file, dst_ext, use_gpu=default_use_gpu)
            # The function writes to base name; if src already has extension, adjust to expected name
            built = os.path.join(TEST_OUTPUT_SUBDIR, f"{os.path.splitext(os.path.basename(src_file))[0]}.{dst_ext}")
            if os.path.exists(built) and built != dst_file:
                try:
                    if os.path.exists(dst_file):
                        os.remove(dst_file)
                except Exception:
                    pass
                # Copy instead of move so the original built source remains for subsequent tests
                shutil.copy2(built, dst_file)

            if not os.path.exists(dst_file):
                raise AssertionError("Output file not created")

            # Probe metadata
            src_meta = probe_media(src_file)
            dst_meta = probe_media(dst_file)
            src_v, src_a = extract_video_audio_meta(src_meta)
            dst_v, dst_a = extract_video_audio_meta(dst_meta)

            # Basic stream existence checks
            if dst_ext in {"mp4", "mkv", "webm", "gif"}:
                assert dst_v is not None, "No video stream in output"
            if dst_ext in {"mp3", "wav"}:
                assert dst_a is not None, "No audio stream in output"

            # Quality-ish checks
            if dst_ext in {"mp4", "mkv", "webm"} and src_v is not None and dst_v is not None:
                src_w = src_v.get("width")
                src_h = src_v.get("height")
                dst_w = dst_v.get("width")
                dst_h = dst_v.get("height")
                assert (src_w == dst_w and src_h == dst_h), f"Resolution changed: {src_w}x{src_h} -> {dst_w}x{dst_h}"
                # Duration consistency
                src_d = float(src_meta.get("format", {}).get("duration", 0) or 0)
                dst_d = float(dst_meta.get("format", {}).get("duration", 0) or 0)
                assert roughly_equal_duration(src_d, dst_d), f"Duration mismatch: {src_d:.2f}s -> {dst_d:.2f}s"
                # File size sanity
                src_size = os.path.getsize(src_file)
                dst_size = os.path.getsize(dst_file)
                low, high = size_ratio_bounds_for_target(dst_ext)
                ratio = (dst_size / src_size) if src_size > 0 else 1.0
                assert (ratio >= low and ratio <= high), (
                    f"File size ratio out of bounds for {dst_ext}: {ratio:.2f} (src {human_size(src_size)} -> dst {human_size(dst_size)})"
                )
                delta_s = abs((dst_d or 0) - (src_d or 0))
                msg = f"PASS {src_ext}->{dst_ext} | {src_w}x{src_h} ~{delta_s:.2f}s Δ | ratio {ratio:.2f}"
                print(msg)
                report_lines.append(msg)
            elif dst_ext == "gif":
                # Functional check only
                assert os.path.getsize(dst_file) > 0, "GIF file is empty"
                size = os.path.getsize(dst_file)
                msg = f"PASS {src_ext}->{dst_ext} | size {human_size(size)}"
                print(msg)
                report_lines.append(msg)
            elif dst_ext in {"mp3", "wav"}:
                assert dst_a is not None, "Expected audio stream"
                size = os.path.getsize(dst_file)
                msg = f"PASS {src_ext}->{dst_ext} | size {human_size(size)}"
                print(msg)
                report_lines.append(msg)
        except Exception as e:
            failures.append(f"{src_ext}->{dst_ext}: {e}")
            err_msg = f"FAIL {src_ext}->{dst_ext} | {e}"
            print(err_msg)
            report_lines.append(err_msg)

    # Write results log
    try:
        with open(RESULTS_LOG_PATH, "a", encoding="utf-8") as f:
            for line in report_lines:
                f.write(line + "\n")
            f.write("\n")
        print(f"Results written to: {RESULTS_LOG_PATH}")
    except Exception as log_err:
        print(f"Warning: failed to write results log: {log_err}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    else:
        print("\nAll conversions passed checks.")


if __name__ == "__main__":
    asyncio.run(main())


