"""Kling 3.0 video generation via Replicate HTTP API — no SDK dependency."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


REPLICATE_API = "https://api.replicate.com/v1"


class KlingClient:
    """Generate video clips with Kling 3.0 via Replicate HTTP API."""

    def __init__(self, api_token: str | None = None):
        token = api_token or os.environ.get("REPLICATE_API_TOKEN")
        if not token:
            raise ValueError(
                "REPLICATE_API_TOKEN environment variable is required.\n"
                "Get a token at: https://replicate.com/account/api-tokens"
            )
        self.token = token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, url: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def _image_to_data_uri(self, image_path: str) -> str:
        ext = Path(image_path).suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "image/png")
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:{mime};base64,{b64}"

    def _wait_for_prediction(self, prediction: dict, poll_interval: int = 5, timeout: int = 600) -> dict:
        """Poll prediction until complete."""
        url = prediction["urls"]["get"]
        start = time.time()
        while time.time() - start < timeout:
            result = self._get(url)
            status = result.get("status")
            if status == "succeeded":
                return result
            elif status in ("failed", "canceled"):
                error = result.get("error", "Unknown error")
                raise RuntimeError(f"Kling prediction failed: {error}")
            time.sleep(poll_interval)
        raise TimeoutError(f"Kling prediction timed out after {timeout}s")

    def generate_segment(
        self,
        start_frame_path: str,
        end_frame_path: str,
        prompt: str,
        output_path: str,
        duration: int = 10,
        model: str = "kwaivgi/kling-v3-omni-video",
    ) -> str:
        """Generate a video segment from start frame to end frame."""
        start_uri = self._image_to_data_uri(start_frame_path)
        end_uri = self._image_to_data_uri(end_frame_path)

        prediction = self._post(
            f"{REPLICATE_API}/models/{model}/predictions",
            {
                "input": {
                    "prompt": prompt,
                    "start_image": start_uri,
                    "end_image": end_uri,
                    "duration": duration,
                    "aspect_ratio": "16:9",
                },
            },
        )

        result = self._wait_for_prediction(prediction)
        output = result.get("output")

        self._download_output(output, output_path)
        return output_path

    def generate_from_image(
        self,
        image_path: str,
        prompt: str,
        output_path: str,
        duration: int = 10,
        model: str = "kwaivgi/kling-v3-omni-video",
    ) -> str:
        """Generate a video from a single start image."""
        image_uri = self._image_to_data_uri(image_path)

        prediction = self._post(
            f"{REPLICATE_API}/models/{model}/predictions",
            {
                "input": {
                    "prompt": prompt,
                    "start_image": image_uri,
                    "duration": duration,
                    "aspect_ratio": "16:9",
                },
            },
        )

        result = self._wait_for_prediction(prediction)
        output = result.get("output")

        self._download_output(output, output_path)
        return output_path

    def _download_output(self, output, output_path: str) -> None:
        """Download the output video from Replicate."""
        if isinstance(output, str):
            url = output
        elif isinstance(output, list) and len(output) > 0:
            url = str(output[0])
        elif isinstance(output, dict) and "url" in output:
            url = output["url"]
        else:
            raise RuntimeError(f"Unexpected Replicate output format: {output}")

        urllib.request.urlretrieve(url, output_path)
