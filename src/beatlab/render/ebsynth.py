"""EbSynth CLI wrapper for style propagation between keyframes."""

from __future__ import annotations

import os
import platform
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

EBSYNTH_DIR = Path.home() / ".beatlab" / "ebsynth"

# GitHub release URLs for ebsynth binary
EBSYNTH_URLS = {
    "Linux": "https://github.com/jamriska/ebsynth/releases/download/v0.6/ebsynth-linux",
    "Darwin": "https://github.com/jamriska/ebsynth/releases/download/v0.6/ebsynth-macos",
}


def ensure_ebsynth(install_dir: str | None = None) -> str:
    """Find or download the EbSynth binary. Returns path to binary.

    Checks: PATH → install_dir → ~/.beatlab/ebsynth → downloads.
    """
    # Check PATH
    for name in ("ebsynth", "EbSynth"):
        try:
            result = subprocess.run(
                ["which", name], capture_output=True, text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass

    # Check install dir
    search_dirs = []
    if install_dir:
        search_dirs.append(Path(install_dir))
    search_dirs.append(EBSYNTH_DIR)

    for d in search_dirs:
        for name in ("ebsynth", "EbSynth"):
            p = d / name
            if p.exists() and os.access(str(p), os.X_OK):
                return str(p)

    # Download
    system = platform.system()
    url = EBSYNTH_URLS.get(system)
    if not url:
        raise RuntimeError(
            f"EbSynth not found and no download available for {system}. "
            f"Install manually from https://ebsynth.com"
        )

    EBSYNTH_DIR.mkdir(parents=True, exist_ok=True)
    binary_path = EBSYNTH_DIR / "ebsynth"
    print(f"Downloading EbSynth for {system}...", flush=True)
    urllib.request.urlretrieve(url, str(binary_path))
    os.chmod(str(binary_path), 0o755)
    return str(binary_path)


def propagate_frame(
    ebsynth_bin: str,
    style_image: str,
    source_at_style: str,
    target_source: str,
    output_path: str,
) -> bool:
    """Propagate style from a keyframe to a target frame.

    Args:
        ebsynth_bin: Path to EbSynth binary.
        style_image: The SD-stylized keyframe.
        source_at_style: The original source frame at the keyframe position.
        target_source: The original source frame at the target position.
        output_path: Where to write the stylized target.

    Returns:
        True if successful, False otherwise.
    """
    try:
        result = subprocess.run(
            [
                ebsynth_bin,
                "-style", style_image,
                "-guide", source_at_style, target_source,
                "-output", output_path,
                "-weight", "1.0",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0 and Path(output_path).exists()
    except Exception:
        return False


def propagate_all(
    source_frames_dir: str,
    styled_keyframes: dict[int, str],
    output_dir: str,
    ebsynth_bin: str | None = None,
    blend_width: int = 4,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """Propagate style from keyframes to all intermediate frames.

    For each pair of consecutive keyframes:
    - Forward propagate from keyframe A toward midpoint
    - Backward propagate from keyframe B toward midpoint
    - Blend at midpoint over blend_width frames
    - Copy keyframe frames directly

    Args:
        source_frames_dir: Directory with original source frames (frame_NNNNNN.png).
        styled_keyframes: Dict mapping frame number → path to styled keyframe.
        output_dir: Directory to write all propagated frames.
        ebsynth_bin: Path to EbSynth binary (auto-detected if None).
        blend_width: Number of frames for cross-fade blending at midpoints.
        progress_callback: Called with (done, total) after each frame.

    Returns:
        Number of successfully propagated frames.
    """
    if ebsynth_bin is None:
        ebsynth_bin = ensure_ebsynth()

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    kf_frames = sorted(styled_keyframes.keys())
    if not kf_frames:
        return 0

    # Find total frame range
    all_source_frames = sorted(
        int(f.split("_")[1].split(".")[0])
        for f in os.listdir(source_frames_dir)
        if f.startswith("frame_") and f.endswith(".png")
    )
    if not all_source_frames:
        return 0

    total = len(all_source_frames)
    done = 0

    def _src(frame_num: int) -> str:
        return os.path.join(source_frames_dir, f"frame_{frame_num:06d}.png")

    def _out(frame_num: int) -> str:
        return os.path.join(output_dir, f"frame_{frame_num:06d}.png")

    # Copy keyframes directly
    import shutil
    for kf in kf_frames:
        out_path = _out(kf)
        if not Path(out_path).exists():
            shutil.copy2(styled_keyframes[kf], out_path)
        done += 1
        if progress_callback:
            progress_callback(done, total)

    # Propagate between each pair of consecutive keyframes
    for i in range(len(kf_frames)):
        kf_a = kf_frames[i]
        kf_b = kf_frames[i + 1] if i + 1 < len(kf_frames) else None

        if kf_b is None:
            # Last keyframe — forward propagate to end
            for f in range(kf_a + 1, all_source_frames[-1] + 1):
                out = _out(f)
                if Path(out).exists():
                    done += 1
                    if progress_callback:
                        progress_callback(done, total)
                    continue
                if not propagate_frame(ebsynth_bin, styled_keyframes[kf_a], _src(kf_a), _src(f), out):
                    # Fallback: copy keyframe
                    shutil.copy2(styled_keyframes[kf_a], out)
                done += 1
                if progress_callback:
                    progress_callback(done, total)
            continue

        mid = (kf_a + kf_b) // 2
        half_blend = blend_width // 2

        # Forward propagate from A: kf_a+1 to mid+half_blend
        for f in range(kf_a + 1, min(mid + half_blend + 1, kf_b)):
            out = _out(f)
            if f <= mid - half_blend:
                # Pure forward — write directly
                if not Path(out).exists():
                    if not propagate_frame(ebsynth_bin, styled_keyframes[kf_a], _src(kf_a), _src(f), out):
                        shutil.copy2(styled_keyframes[kf_a], out)
            else:
                # In blend zone — write to temp for blending later
                fwd_path = out + ".fwd"
                if not Path(fwd_path).exists():
                    if not propagate_frame(ebsynth_bin, styled_keyframes[kf_a], _src(kf_a), _src(f), fwd_path):
                        shutil.copy2(styled_keyframes[kf_a], fwd_path)
            done += 1
            if progress_callback:
                progress_callback(done, total)

        # Backward propagate from B: kf_b-1 down to mid-half_blend
        for f in range(kf_b - 1, max(mid - half_blend - 1, kf_a), -1):
            out = _out(f)
            if f >= mid + half_blend:
                # Pure backward — write directly
                if not Path(out).exists():
                    if not propagate_frame(ebsynth_bin, styled_keyframes[kf_b], _src(kf_b), _src(f), out):
                        shutil.copy2(styled_keyframes[kf_b], out)
            else:
                # In blend zone — write to temp
                bwd_path = out + ".bwd"
                if not Path(bwd_path).exists():
                    if not propagate_frame(ebsynth_bin, styled_keyframes[kf_b], _src(kf_b), _src(f), bwd_path):
                        shutil.copy2(styled_keyframes[kf_b], bwd_path)

        # Blend zone: cross-fade between forward and backward
        blend_start = mid - half_blend
        blend_end = mid + half_blend
        for f in range(max(blend_start, kf_a + 1), min(blend_end + 1, kf_b)):
            out = _out(f)
            if Path(out).exists():
                continue
            fwd_path = out + ".fwd"
            bwd_path = out + ".bwd"
            if Path(fwd_path).exists() and Path(bwd_path).exists():
                # Linear cross-fade
                t = (f - blend_start) / max(1, blend_end - blend_start)
                _blend_images(fwd_path, bwd_path, out, t)
                os.unlink(fwd_path)
                os.unlink(bwd_path)
            elif Path(fwd_path).exists():
                os.rename(fwd_path, out)
            elif Path(bwd_path).exists():
                os.rename(bwd_path, out)

    return done


def _blend_images(img_a: str, img_b: str, output: str, t: float) -> None:
    """Blend two images: output = (1-t)*A + t*B.

    Uses PIL if available, otherwise just picks the closer one.
    """
    try:
        from PIL import Image
        a = Image.open(img_a)
        b = Image.open(img_b)
        blended = Image.blend(a, b, t)
        blended.save(output)
    except ImportError:
        # No PIL — pick whichever is closer
        import shutil
        shutil.copy2(img_a if t < 0.5 else img_b, output)
