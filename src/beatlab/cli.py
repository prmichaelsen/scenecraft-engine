"""CLI interface for beatlab."""

from __future__ import annotations

import json
import sys

import click


@click.group()
@click.version_option(package_name="davinci-beat-lab")
def main():
    """beatlab — AI-powered beat detection and visual effects for DaVinci Resolve."""
    pass


@main.command()
@click.argument("audio_file", type=click.Path(exists=True))
@click.option("--fps", default=30.0, type=float, help="Timeline frame rate (default: 30)")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output JSON file (default: stdout)")
@click.option("--sr", default=22050, type=int, help="Sample rate for analysis (default: 22050)")
def analyze(audio_file: str, fps: float, output: str | None, sr: int):
    """Analyze an audio file and produce a beat map JSON."""
    from beatlab.analyzer import analyze_audio
    from beatlab.beat_map import create_beat_map, save_beat_map

    click.echo(f"Analyzing: {audio_file}", err=True)
    analysis = analyze_audio(audio_file, sr=sr)
    click.echo(
        f"  Tempo: {analysis['tempo']:.1f} BPM | "
        f"Beats: {len(analysis['beats'])} | "
        f"Onsets: {len(analysis['onsets'])} | "
        f"Duration: {analysis['duration']:.1f}s",
        err=True,
    )

    beat_map = create_beat_map(analysis, fps=fps, source_file=audio_file)

    if output:
        save_beat_map(beat_map, output)
        click.echo(f"  Beat map written to: {output}", err=True)
    else:
        json.dump(beat_map, sys.stdout, indent=2)
        sys.stdout.write("\n")
