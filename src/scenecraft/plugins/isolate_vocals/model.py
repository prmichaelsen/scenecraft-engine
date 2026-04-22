"""DeepFilterNet3 loader + denoise inference.

Lazy — the model loads on first call and is cached for the life of the process.
``deepfilternet`` is an optional dep (``pip install scenecraft-engine[plugins]``);
if it's missing, the ImportError surfaces through the plugin's job manager as a
``fail_job`` with a friendly message telling the user what to install.

Tests patch ``denoise_wav`` with a fake that just copies input → output, so CI
does not need the real model binary.
"""

from __future__ import annotations

from pathlib import Path


_state: dict = {"loaded": False}


def _ensure_model() -> None:
    """Idempotent: loads DFN3 on the first call."""
    if _state["loaded"]:
        return
    from df.enhance import init_df, enhance, load_audio, save_audio  # type: ignore[import-not-found]

    model, df_state, _ = init_df()
    _state.update(
        loaded=True,
        model=model,
        enhance=enhance,
        load=load_audio,
        save=save_audio,
        sr=df_state.sr(),
        df_state=df_state,
    )


def denoise_wav(in_path: Path, out_path: Path) -> None:
    """Run DFN3 speech enhancement on a mono WAV.

    Output preserves duration and sample rate. Both paths are plain files on
    disk — we lean on ``df``'s own I/O helpers to keep the pipeline simple.
    """
    _ensure_model()
    audio, _sr = _state["load"](str(in_path), sr=_state["sr"])
    enhanced = _state["enhance"](_state["model"], _state["df_state"], audio)
    _state["save"](str(out_path), enhanced, _state["sr"])
