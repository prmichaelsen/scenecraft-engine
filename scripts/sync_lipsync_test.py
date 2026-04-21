#!/usr/bin/env python3
"""Throwaway test of Sync.so lip-sync — direct multipart upload, no external hosting.

Usage:
  python3 scripts/sync_lipsync_test.py <video_path> "<script text>"
"""
import os
import sys
import time
import json
import subprocess

SYNC_API_KEY = os.environ.get("SYNC_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

if not SYNC_API_KEY:
    sys.exit("ERROR: SYNC_API_KEY env var not set.")
if len(sys.argv) < 3:
    sys.exit(f"Usage: {sys.argv[0]} <video_path> <script text>")

video_path = sys.argv[1]
script = sys.argv[2]
output_path = "/tmp/sync_lipsync_output.mp4"

if not os.path.exists(video_path):
    sys.exit(f"Video not found: {video_path}")
size_mb = os.path.getsize(video_path) / 1_000_000
if size_mb > 20:
    sys.exit(f"Video too large ({size_mb:.1f}MB) — Sync.so direct upload max is 20MB")

print(f"Uploading {video_path} ({size_mb:.1f}MB) + TTS (voice {ELEVENLABS_VOICE_ID})")
print(f"Script: {script!r}\n")

# Multipart submit — video as file, audio as TTS input
# The `input` field is stringified JSON with the audio provider block
input_json = json.dumps([
    {
        "type": "text",
        "provider": {
            "name": "elevenlabs",
            "voiceId": ELEVENLABS_VOICE_ID,
            "script": script,
        },
    },
])
options_json = json.dumps({"sync_mode": "cut_off"})

cmd = [
    "curl", "-sS", "-X", "POST",
    "https://api.sync.so/v2/generate",
    "-H", f"x-api-key: {SYNC_API_KEY}",
    "-F", "model=lipsync-2",
    "-F", f"video=@{video_path};type=video/mp4",
    "-F", f"input={input_json};type=application/json",
    "-F", f"options={options_json};type=application/json",
]
print("Submitting...")
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode != 0:
    sys.exit(f"curl failed: {result.stderr}")

try:
    job = json.loads(result.stdout)
except json.JSONDecodeError:
    sys.exit(f"Bad response: {result.stdout!r}")

if "id" not in job:
    sys.exit(f"No job id in response: {json.dumps(job, indent=2)}")

job_id = job["id"]
print(f"Job: {job_id}  status={job.get('status')}")

# Poll
poll_cmd_base = ["curl", "-sS", "-H", f"x-api-key: {SYNC_API_KEY}",
                 f"https://api.sync.so/v2/generate/{job_id}"]
start = time.time()
while True:
    time.sleep(5)
    r = subprocess.run(poll_cmd_base, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Poll failed: {r.stderr}")
        continue
    try:
        job = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"Bad poll response: {r.stdout[:200]!r}")
        continue

    status = (job.get("status") or "").upper()
    elapsed = time.time() - start
    print(f"[{elapsed:.0f}s] status={status}")

    if status == "COMPLETED":
        out_url = job.get("outputUrl") or job.get("output_url")
        if not out_url:
            print(f"No output URL: {json.dumps(job, indent=2)}")
            break
        print(f"\nDownloading {out_url}...")
        subprocess.run(["curl", "-sSL", out_url, "-o", output_path], check=True)
        size = os.path.getsize(output_path) / 1_000_000
        print(f"Saved {output_path} ({size:.1f}MB)")
        break

    if status in ("FAILED", "REJECTED", "CANCELED"):
        sys.exit(f"Job failed: {json.dumps(job, indent=2)}")

    if elapsed > 600:
        sys.exit("Timeout after 10 minutes")
