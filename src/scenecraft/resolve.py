"""DaVinci Resolve headless integration — Fusion comp import and render."""

from __future__ import annotations

import os
import sys
import time
import subprocess
from pathlib import Path


# Ensure Resolve scripting API is importable
_RESOLVE_SCRIPT_API = "/opt/resolve/Developer/Scripting"
_RESOLVE_SCRIPT_LIB = "/opt/resolve/libs/Fusion/fusionscript.so"
_RESOLVE_MODULES = os.path.join(_RESOLVE_SCRIPT_API, "Modules")

if _RESOLVE_MODULES not in sys.path:
    sys.path.insert(0, _RESOLVE_MODULES)

os.environ.setdefault("RESOLVE_SCRIPT_API", _RESOLVE_SCRIPT_API)
os.environ.setdefault("RESOLVE_SCRIPT_LIB", _RESOLVE_SCRIPT_LIB)


def _get_resolve():
    """Get the Resolve scripting API object. Returns None if Resolve isn't running."""
    try:
        import DaVinciResolveScript as dvr_script
        return dvr_script.scriptapp("Resolve")
    except (ImportError, Exception):
        return None


def launch_headless(timeout: int = 60) -> object:
    """Launch Resolve in headless mode and wait for the API to become available.

    Args:
        timeout: Max seconds to wait for Resolve to initialize.

    Returns:
        The Resolve scripting API object.

    Raises:
        RuntimeError: If Resolve fails to start within the timeout.
    """
    resolve = _get_resolve()
    if resolve is not None:
        return resolve

    resolve_bin = Path("/opt/resolve/bin/resolve")
    if not resolve_bin.exists():
        raise FileNotFoundError(f"Resolve binary not found: {resolve_bin}")

    print(f"Launching DaVinci Resolve headless...")
    subprocess.Popen(
        [str(resolve_bin), "-nogui"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    start = time.time()
    while time.time() - start < timeout:
        resolve = _get_resolve()
        if resolve is not None:
            version = resolve.GetVersionString()
            print(f"  Connected to DaVinci Resolve {version}")
            return resolve
        time.sleep(2)

    raise RuntimeError(f"Resolve did not start within {timeout}s")


def connect(timeout: int = 60) -> "ResolveSession":
    """Connect to Resolve (launching headless if needed) and return a session."""
    resolve = launch_headless(timeout=timeout)
    return ResolveSession(resolve)


class ResolveSession:
    """Persistent session wrapping the Resolve scripting API.

    Focused on the immediate need: import Fusion comps and render.
    """

    def __init__(self, resolve):
        self._resolve = resolve
        self._pm = resolve.GetProjectManager()

    @property
    def resolve(self):
        return self._resolve

    @property
    def project(self):
        return self._pm.GetCurrentProject()

    @property
    def timeline(self):
        proj = self.project
        return proj.GetCurrentTimeline() if proj else None

    def get_version(self) -> str:
        """Get Resolve version string."""
        return self._resolve.GetVersionString()

    def get_project_name(self) -> str:
        """Get current project name."""
        proj = self.project
        return proj.GetName() if proj else "(no project)"

    def get_timeline_info(self) -> dict:
        """Get current timeline info: name, fps, start/end frame, duration."""
        tl = self.timeline
        if tl is None:
            return {"error": "No timeline loaded"}

        proj = self.project
        fps = float(proj.GetSetting("timelineFrameRate") or 30)
        start = tl.GetStartFrame()
        end = tl.GetEndFrame()
        duration_frames = end - start
        duration_sec = duration_frames / fps if fps > 0 else 0

        return {
            "name": tl.GetName(),
            "fps": fps,
            "start_frame": start,
            "end_frame": end,
            "duration_frames": duration_frames,
            "duration_sec": round(duration_sec, 3),
            "track_count_video": tl.GetTrackCount("video"),
            "track_count_audio": tl.GetTrackCount("audio"),
        }

    def get_timeline_items(self, track_index: int = 1) -> list:
        """Get all timeline items on the specified video track (1-based)."""
        tl = self.timeline
        if tl is None:
            return []
        items = tl.GetItemListInTrack("video", track_index)
        return items or []

    def import_fusion_comp(
        self,
        setting_path: str,
        track_index: int = 1,
        item_index: int = 0,
    ) -> bool:
        """Import a Fusion .setting file into a timeline item.

        Args:
            setting_path: Absolute path to the .setting file.
            track_index: Video track number (1-based).
            item_index: Index into the track's items (0-based). Default: first item.

        Returns:
            True if import succeeded.
        """
        path = Path(setting_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f".setting file not found: {path}")

        items = self.get_timeline_items(track_index)
        if not items:
            raise RuntimeError(f"No items on video track {track_index}")
        if item_index >= len(items):
            raise IndexError(f"Item index {item_index} out of range (track has {len(items)} items)")

        item = items[item_index]
        comp = item.ImportFusionComp(str(path))
        if comp:
            print(f"  Fusion comp imported into '{item.GetName()}' from {path.name}")
            return True
        else:
            print(f"  Failed to import Fusion comp into '{item.GetName()}'")
            return False

    def add_render_job(self, output_dir: str, filename: str = "render", format: str = "mp4") -> str | None:
        """Add a render job to the queue with current timeline settings.

        Args:
            output_dir: Directory for rendered output.
            filename: Output filename (without extension).
            format: Render format (mp4, mov, etc.).

        Returns:
            Job ID string, or None on failure.
        """
        proj = self.project
        if proj is None:
            return None

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        proj.SetRenderSettings({
            "TargetDir": output_dir,
            "CustomName": filename,
        })

        job_id = proj.AddRenderJob()
        if job_id:
            print(f"  Render job added: {job_id}")
        return job_id

    def start_render(self, job_ids: list[str] | None = None) -> bool:
        """Start rendering queued jobs.

        Args:
            job_ids: Specific job IDs to render. None = render all queued.

        Returns:
            True if rendering started.
        """
        proj = self.project
        if proj is None:
            return False

        if job_ids:
            return proj.StartRendering(*job_ids)
        else:
            return proj.StartRendering()

    def get_render_status(self, job_id: str) -> dict:
        """Get render job status and progress.

        Returns:
            Dict with 'JobStatus' and 'CompletionPercentage'.
        """
        proj = self.project
        if proj is None:
            return {"JobStatus": "Error", "CompletionPercentage": 0}
        return proj.GetRenderJobStatus(job_id)

    def is_rendering(self) -> bool:
        """Check if any render is in progress."""
        proj = self.project
        return proj.IsRenderingInProgress() if proj else False

    def wait_for_render(self, job_id: str, poll_interval: float = 1.0) -> dict:
        """Block until a render job completes, printing progress.

        Returns:
            Final render status dict.
        """
        while True:
            status = self.get_render_status(job_id)
            pct = status.get("CompletionPercentage", 0)
            job_status = status.get("JobStatus", "Unknown")

            if job_status in ("Complete", "Failed", "Cancelled"):
                print(f"  Render {job_status}: {pct}%")
                return status

            print(f"  Rendering: {pct}%", end="\r")
            time.sleep(poll_interval)
