"""Audio stem separation via Demucs and per-stem analysis."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path


STEM_NAMES = ("drums", "bass", "vocals", "other")

DEMUCS_IMAGE = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def separate_stems_remote(
    audio_path: str,
    output_dir: str,
    vast_manager: object,
) -> dict[str, str]:
    """Run Demucs on a Vast.ai GPU instance to separate audio into stems.

    Args:
        audio_path: Path to input audio file (WAV).
        output_dir: Local directory to store output stems.
        vast_manager: VastAIManager instance.

    Returns:
        Dict mapping stem name to local file path, e.g. {"drums": "/path/drums.wav", ...}.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Check cache
    expected = {s: str(out / f"{s}.wav") for s in STEM_NAMES}
    if all(Path(p).exists() for p in expected.values()):
        _log("Stems: using cached")
        return expected

    _log("Stems: separating audio via Demucs on Vast.ai...")

    # Get or create a stems-specific instance (cheap, 8GB VRAM)
    instance_id, reused = vast_manager.get_or_create_instance(
        instance_key="stems",
        image=DEMUCS_IMAGE,
        min_vram_gb=8,
        max_price_hr=2.0,
        disk_gb=30,
    )

    if not reused:
        _log(f"  Waiting for instance {instance_id} to be ready...")
        vast_manager.wait_until_ready(instance_id)

    _log(f"  Instance {instance_id} ready (reused={reused})")

    # Wait for SSH to be reachable
    import time as _time
    host, port = vast_manager.get_ssh_info(instance_id)
    for attempt in range(12):
        try:
            vast_manager.ssh_run(instance_id, "echo ok", timeout=15)
            break
        except Exception:
            _log(f"  Waiting for SSH... (attempt {attempt + 1}/12)")
            _time.sleep(10)
    else:
        raise RuntimeError(f"SSH not reachable on instance {instance_id} after 2 minutes")

    # Install demucs + deps — reinstall torch matching the GPU's CUDA arch
    _log("  Installing demucs + dependencies...")
    vast_manager.ssh_run(
        instance_id,
        "apt-get update -qq && apt-get install -y -qq ffmpeg > /dev/null 2>&1;"
        " pip install -q demucs soundfile lameenc 2>/dev/null || pip install demucs soundfile lameenc",
        timeout=600,
    )

    # Upload audio file — stage it in a temp dir so upload_files works (it syncs dirs)
    import tempfile
    remote_work = "/workspace/stems_work"
    audio_name = Path(audio_path).name

    with tempfile.TemporaryDirectory() as staging:
        shutil.copy2(audio_path, Path(staging) / audio_name)
        _log(f"  Uploading {audio_name}...")
        vast_manager.upload_files(instance_id, staging, remote_work)

    # Run demucs — use subprocess directly to capture stderr
    # Use --mp3 to avoid torchcodec issues, convert back to wav after download
    _log("  Running Demucs (htdemucs model)...")
    host, port = vast_manager.get_ssh_info(instance_id)
    ssh_opts = vast_manager._ssh_opts(port)
    demucs_cmd = f"cd {remote_work} && python -m demucs -n htdemucs -d cpu --mp3 --mp3-bitrate 320 -o output {audio_name} 2>&1"
    import subprocess
    demucs_result = subprocess.run(
        f'{ssh_opts} root@{host} "{demucs_cmd}"',
        shell=True, capture_output=True, text=True, timeout=1800,
    )
    _log(f"  Demucs stdout: {demucs_result.stdout[-500:] if demucs_result.stdout else '(empty)'}")
    if demucs_result.stderr:
        _log(f"  Demucs stderr: {demucs_result.stderr[-500:]}")

    # Verify stems exist on remote before downloading
    audio_stem = Path(audio_name).stem
    remote_stems = f"{remote_work}/output/htdemucs/{audio_stem}"
    ls_result = vast_manager.ssh_run(instance_id, f"ls -la {remote_stems}/", timeout=15)
    _log(f"  Remote stems: {ls_result.strip() if ls_result else '(empty dir)'}")
    if not ls_result or "drums" not in ls_result:
        raise RuntimeError(
            f"Demucs did not produce stems. Remote dir contents:\n{ls_result}\n"
            f"Demucs output:\n{demucs_result.stdout[-1000:]}"
        )

    # Download stems (may be .mp3 if --mp3 was used)
    _log("  Downloading stems...")
    vast_manager.download_files(instance_id, remote_stems, str(out))

    # Convert mp3 stems to wav if needed
    for stem in STEM_NAMES:
        wav_path = out / f"{stem}.wav"
        mp3_path = out / f"{stem}.mp3"
        if wav_path.exists():
            continue
        if mp3_path.exists():
            _log(f"  Converting {stem}.mp3 → wav...")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3_path), "-acodec", "pcm_s16le",
                 "-ar", "44100", "-ac", "2", str(wav_path)],
                check=True, capture_output=True,
            )
        else:
            raise RuntimeError(f"Failed to download stem: {stem} (no .wav or .mp3 found)")

    _log("  Stem separation complete")
    return expected


def separate_stems_local(
    audio_path: str,
    output_dir: str,
) -> dict[str, str]:
    """Run Demucs locally (CPU — slow for long files, use for testing only).

    Args:
        audio_path: Path to input audio file.
        output_dir: Local directory to store output stems.

    Returns:
        Dict mapping stem name to local file path.
    """
    import subprocess

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    expected = {s: str(out / f"{s}.wav") for s in STEM_NAMES}
    if all(Path(p).exists() for p in expected.values()):
        _log("Stems: using cached")
        return expected

    _log("Stems: separating audio via Demucs (local CPU — this will be slow)...")
    result = subprocess.run(
        ["python", "-m", "demucs", "-n", "htdemucs", "--two-stems=None",
         "-o", str(out / "demucs_output"), audio_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Demucs failed: {result.stderr[-500:]}")

    # Move stems to expected locations
    audio_stem = Path(audio_path).stem
    demucs_dir = out / "demucs_output" / "htdemucs" / audio_stem
    for stem in STEM_NAMES:
        src = demucs_dir / f"{stem}.wav"
        dst = out / f"{stem}.wav"
        if src.exists():
            shutil.move(str(src), str(dst))
        else:
            raise RuntimeError(f"Demucs did not produce {stem}.wav")

    _log("  Stem separation complete")
    return expected


MULTIMODEL_STEMS = {
    "vocals": "mdx23c_instvoc",
    "kick": "mdx23c_drumsep",
    "snare": "mdx23c_drumsep",
    "hh": "mdx23c_drumsep",
    "ride": "mdx23c_drumsep",
    "crash": "mdx23c_drumsep",
    "toms": "mdx23c_drumsep",
    "bass": "demucs_6s",
    "guitar": "demucs_6s",
    "piano": "demucs_6s",
    "other": "demucs_6s",
}


def separate_stems_multimodel(audio_path: str, output_dir: str) -> dict[str, str]:
    """Run the 3-model stem separation pipeline locally.

    Pipeline: MDX23C-InstVoc → {vocals, instrumental}
              DrumSep(instrumental) → {kick, snare, hh, ride, crash, toms}
              Demucs 6s(instrumental) → {bass, guitar, piano, other}

    Args:
        audio_path: Path to input audio file.
        output_dir: Root output directory for all stems.

    Returns:
        Dict mapping stem name to WAV path for all 11 stems.
    """
    import subprocess
    import sys

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    instvoc_dir = out / "mdx23c_instvoc"
    drumsep_dir = out / "mdx23c_drumsep"
    demucs_dir = out / "demucs_6s"

    # Check if all stems already exist (cache) — need at least 10 stems
    result_paths = _multimodel_result_paths(out, Path(audio_path).stem)
    if len(result_paths) >= 10 and all(Path(p).exists() for p in result_paths.values()):
        _log(f"Multi-model stems: using cached ({len(result_paths)} stems)")
        return result_paths

    # ── Step 1: MDX23C-InstVoc ──
    vocals_path, instrumental_path = _run_instvoc(audio_path, instvoc_dir)

    # ── Step 2: DrumSep + Demucs 6s on instrumental (sequential to avoid OOM) ──
    drumsep_paths = _run_drumsep(instrumental_path, drumsep_dir)
    demucs_paths = _run_demucs_6s(instrumental_path, demucs_dir)

    # ── Step 3: Collect all paths ──
    result = {"vocals": vocals_path}
    result.update(drumsep_paths)
    result.update(demucs_paths)

    _log(f"Multi-model separation complete: {len(result)} stems")
    return result


def _multimodel_result_paths(out: Path, audio_stem: str) -> dict[str, str]:
    """Build expected result paths for cache checking."""
    instvoc_dir = out / "mdx23c_instvoc"
    drumsep_dir = out / "mdx23c_drumsep"
    demucs_dir = out / "demucs_6s"

    # audio-separator names files with the input filename prefix
    # Demucs 6s names files by stem name in a subdirectory
    paths = {}

    # InstVoc vocals
    for f in instvoc_dir.glob("*Vocals*") if instvoc_dir.exists() else []:
        if f.suffix == ".wav":
            paths["vocals"] = str(f)
            break

    # DrumSep
    for drum in ("kick", "snare", "hh", "ride", "crash", "toms"):
        for f in drumsep_dir.glob(f"*({drum})*") if drumsep_dir.exists() else []:
            if f.suffix == ".wav":
                paths[drum] = str(f)
                break

    # Demucs 6s — look in the htdemucs_6s subdirectory
    demucs_stem_dir = None
    if demucs_dir.exists():
        for d in demucs_dir.rglob("htdemucs_6s"):
            for sub in d.iterdir():
                if sub.is_dir():
                    demucs_stem_dir = sub
                    break
            break

    if demucs_stem_dir:
        for stem in ("bass", "guitar", "piano", "other"):
            wav = demucs_stem_dir / f"{stem}.wav"
            mp3 = demucs_stem_dir / f"{stem}.mp3"
            if wav.exists():
                paths[stem] = str(wav)
            elif mp3.exists():
                paths[stem] = str(mp3)  # will need conversion

    return paths


def _run_instvoc(audio_path: str, output_dir: Path) -> tuple[str, str]:
    """Run MDX23C-InstVoc-HQ. Returns (vocals_path, instrumental_path)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check cache
    vocals_files = list(output_dir.glob("*Vocals*.wav"))
    inst_files = list(output_dir.glob("*Instrumental*.wav"))
    if vocals_files and inst_files:
        _log("  InstVoc: using cached")
        return str(vocals_files[0]), str(inst_files[0])

    _log("  Step 1: MDX23C-InstVoc (vocals + instrumental)...")
    from audio_separator.separator import Separator
    sep = Separator(output_dir=str(output_dir), model_file_dir="/tmp/audio-separator-models/")
    sep.load_model("MDX23C-8KFFT-InstVoc_HQ.ckpt")
    stems = sep.separate(audio_path)
    _log(f"    Produced: {stems}")

    # Find outputs
    vocals_files = list(output_dir.glob("*Vocals*.wav"))
    inst_files = list(output_dir.glob("*Instrumental*.wav"))
    if not vocals_files or not inst_files:
        raise RuntimeError(f"InstVoc did not produce expected stems in {output_dir}")

    return str(vocals_files[0]), str(inst_files[0])


def _run_drumsep(instrumental_path: str, output_dir: Path) -> dict[str, str]:
    """Run MDX23C-DrumSep on instrumental. Returns {kick, snare, hh, ride, crash, toms} paths."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check cache
    cached = {}
    for drum in ("kick", "snare", "hh", "ride", "crash", "toms"):
        matches = list(output_dir.glob(f"*({drum})*.wav"))
        if matches:
            cached[drum] = str(matches[0])
    if len(cached) == 6:
        _log("  DrumSep: using cached")
        return cached

    _log("  Step 2a: MDX23C-DrumSep (kick/snare/hh/ride/crash/toms)...")
    from audio_separator.separator import Separator
    sep = Separator(output_dir=str(output_dir), model_file_dir="/tmp/audio-separator-models/")
    sep.load_model("MDX23C-DrumSep-aufr33-jarredou.ckpt")
    stems = sep.separate(instrumental_path)
    _log(f"    Produced: {stems}")

    result = {}
    for drum in ("kick", "snare", "hh", "ride", "crash", "toms"):
        matches = list(output_dir.glob(f"*({drum})*.wav"))
        if matches:
            result[drum] = str(matches[0])
        else:
            _log(f"    Warning: {drum} stem not found")

    return result


def _run_demucs_6s(instrumental_path: str, output_dir: Path) -> dict[str, str]:
    """Run Demucs htdemucs_6s on instrumental. Returns {bass, guitar, piano, other} paths."""
    import subprocess
    import sys

    output_dir.mkdir(parents=True, exist_ok=True)

    # Find the demucs output subdirectory
    inst_stem = Path(instrumental_path).stem

    def _find_demucs_stems():
        for d in output_dir.rglob("htdemucs_6s"):
            for sub in d.iterdir():
                if sub.is_dir():
                    result = {}
                    for stem in ("bass", "guitar", "piano", "other"):
                        wav = sub / f"{stem}.wav"
                        mp3 = sub / f"{stem}.mp3"
                        if wav.exists():
                            result[stem] = str(wav)
                        elif mp3.exists():
                            result[stem] = str(mp3)
                    if len(result) >= 4:
                        return result
        return None

    # Check cache
    cached = _find_demucs_stems()
    if cached:
        _log("  Demucs 6s: using cached")
        # Convert mp3 to wav if needed
        for stem, path in cached.items():
            if path.endswith(".mp3"):
                wav_path = path.replace(".mp3", ".wav")
                if not Path(wav_path).exists():
                    _log(f"    Converting {stem}.mp3 → wav...")
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", path, "-acodec", "pcm_s16le",
                         "-ar", "44100", "-ac", "2", wav_path],
                        check=True, capture_output=True,
                    )
                cached[stem] = wav_path
        return cached

    _log("  Step 2b: Demucs htdemucs_6s (bass/guitar/piano/other)...")
    result = subprocess.run(
        [sys.executable, "-m", "demucs", "-n", "htdemucs_6s", "-d", "cpu",
         "--mp3", "--mp3-bitrate", "320", "-o", str(output_dir), instrumental_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Demucs 6s failed: {result.stderr[-500:]}")

    stems = _find_demucs_stems()
    if not stems:
        raise RuntimeError(f"Demucs 6s did not produce expected stems in {output_dir}")

    # Convert mp3 to wav
    for stem, path in stems.items():
        if path.endswith(".mp3"):
            wav_path = path.replace(".mp3", ".wav")
            _log(f"    Converting {stem}.mp3 → wav...")
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-acodec", "pcm_s16le",
                 "-ar", "44100", "-ac", "2", wav_path],
                check=True, capture_output=True,
            )
            stems[stem] = wav_path

    _log(f"    Demucs 6s complete: {list(stems.keys())}")
    return stems


def _detect_onsets(y, sr_out, hop_length=512) -> list[dict]:
    """Detect onsets using librosa directly (no beat_this)."""
    import librosa
    import numpy as np

    onset_env = librosa.onset.onset_strength(y=y, sr=sr_out, hop_length=hop_length)
    onset_frames = librosa.onset.onset_detect(
        y=y, sr=sr_out, hop_length=hop_length, onset_envelope=onset_env, backtrack=True,
    )
    max_idx = len(onset_env) - 1
    onset_frames = np.clip(onset_frames, 0, max_idx) if len(onset_frames) > 0 else onset_frames
    onset_times = librosa.frames_to_time(onset_frames, sr=sr_out, hop_length=hop_length)
    onset_strengths = onset_env[onset_frames] if len(onset_frames) > 0 else np.array([])
    if len(onset_strengths) > 0 and onset_strengths.max() > 0:
        onset_strengths = onset_strengths / onset_strengths.max()

    return [
        {"time": float(t), "strength": float(s)}
        for t, s in zip(onset_times, onset_strengths)
    ]


def analyze_stem(path: str, stem_type: str, sr: int = 22050) -> dict:
    """Analyze a single audio stem with strategy appropriate to its type.

    Args:
        path: Path to stem WAV file.
        stem_type: One of "drums", "bass", "vocals", "other".
        sr: Sample rate for analysis.

    Returns:
        Analysis dict with keys appropriate to the stem type.
    """
    from scenecraft.analyzer import load_audio, detect_drops, detect_presence, detect_sections
    import librosa
    import numpy as np

    if stem_type == "drums":
        # Onset detection only — no beat_this grid snapping.
        # Isolated drum track gives clean onsets that are the actual hits.
        y, sr_out = load_audio(path, sr=sr)
        hop_length = 512
        onset_env = librosa.onset.onset_strength(y=y, sr=sr_out, hop_length=hop_length)
        onset_frames = librosa.onset.onset_detect(
            y=y, sr=sr_out, hop_length=hop_length, onset_envelope=onset_env, backtrack=True,
        )
        max_idx = len(onset_env) - 1
        onset_frames = np.clip(onset_frames, 0, max_idx) if len(onset_frames) > 0 else onset_frames
        onset_times = librosa.frames_to_time(onset_frames, sr=sr_out, hop_length=hop_length)
        onset_strengths = onset_env[onset_frames] if len(onset_frames) > 0 else np.array([])
        if len(onset_strengths) > 0 and onset_strengths.max() > 0:
            onset_strengths = onset_strengths / onset_strengths.max()

        onsets = [
            {"time": float(t), "strength": float(s)}
            for t, s in zip(onset_times, onset_strengths)
        ]

        # Estimate tempo from onset intervals
        if len(onset_times) >= 2:
            intervals = np.diff(onset_times)
            median_interval = float(np.median(intervals))
            tempo = 60.0 / median_interval if median_interval > 0 else 120.0
        else:
            tempo = 120.0

        sections = detect_sections(y, sr_out)

        return {
            "tempo": tempo,
            "onsets": onsets,
            "sections": sections,
        }

    elif stem_type == "bass":
        # Onsets + drop detection
        y, sr_out = load_audio(path, sr=sr)
        onsets = _detect_onsets(y, sr_out)
        drops = detect_drops(y, sr_out)
        return {
            "onsets": onsets,
            "drops": drops,
        }

    elif stem_type == "vocals":
        # Onsets + presence detection
        y, sr_out = load_audio(path, sr=sr)
        onsets = _detect_onsets(y, sr_out)
        presence = detect_presence(y, sr_out)
        return {
            "onsets": onsets,
            "presence": presence,
        }

    else:  # "other"
        # Onsets only
        y, sr_out = load_audio(path, sr=sr)
        onsets = _detect_onsets(y, sr_out)
        return {
            "onsets": onsets,
        }


def analyze_all_stems(stem_paths: dict[str, str], sr: int = 22050) -> dict:
    """Analyze all stems and return a dict suitable for beat_map enrichment.

    Args:
        stem_paths: Dict mapping stem name to WAV path.
        sr: Sample rate for analysis.

    Returns:
        Dict of {stem_name: analysis_dict}.
    """
    results = {}
    for stem_name, path in stem_paths.items():
        if not Path(path).exists():
            _log(f"  Warning: stem {stem_name} not found at {path}, skipping")
            continue
        _log(f"  Analyzing {stem_name} stem...")
        results[stem_name] = analyze_stem(path, stem_name, sr=sr)
        # Log summary
        analysis = results[stem_name]
        parts = []
        if "beats" in analysis:
            parts.append(f"{len(analysis['beats'])} beats")
        if "onsets" in analysis:
            parts.append(f"{len(analysis['onsets'])} onsets")
        if "drops" in analysis:
            parts.append(f"{len(analysis['drops'])} drops")
        if "presence" in analysis:
            parts.append(f"{len(analysis['presence'])} vocal regions")
        _log(f"    {stem_name}: {', '.join(parts)}")

    return results
