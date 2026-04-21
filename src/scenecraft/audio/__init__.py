"""Audio track and clip primitives for M9.

Modules:
    extract  — ffmpeg/ffprobe-based audio-stream extraction from video files
    routing  — slot-matching (video z_order ↔ audio display_order) on insert
    curves   — dB volume curve evaluation + helpers
    mixdown  — multi-track audio render pipeline (sum + crossfades)
"""
