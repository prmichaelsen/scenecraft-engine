"""Backend mirror of the frontend effect-type registry (spec local.effect-curves-macro-panel.md §R7-R9).

The frontend (``scenecraft/src/lib/audio-effect-types.ts``) owns the runtime
``build()`` factories that wire WebAudio nodes. The backend only needs three
things from the registry to do its R_V1 POST validation:

  * Which ``effect_type`` strings are valid (R8 enumerated list — 17 types).
  * Which params are animatable (R9 — ``effect_curves`` may only reference
    animatable params; attempts to animate a static param return HTTP 400).
  * Per-param ranges (so future endpoints can validate curve-point values
    against the param's nominal range; currently used only for documentation).

Keep this dict in lockstep with the TypeScript registry. A drift between the
two surfaces would either (a) let the backend accept a curve that the mixer
silently drops, or (b) have the backend reject a param the mixer does support.
Both are confusing; the registries MUST agree.

Spec R8a: the synthetic ``__send`` effect_type is NOT in this registry. It is
recognised ONLY by the ``effect_curves`` path (for animating per-bus send
levels) and is explicitly rejected by POST /track-effects.
"""

from __future__ import annotations

from typing import Any


# param spec fields: { animatable: bool, range: [min, max], scale: str, default: float }
# Values are normalized 0..1 at the DB layer (R6); ``range`` here is the
# NATIVE-unit range used by the frontend for display and for scaling curve
# points back to AudioParam-native values at schedule time (R17). Keep the
# native ranges in sync with the TS file for UX correctness; the backend
# itself does not use them for validation yet.


def _p(
    *,
    animatable: bool,
    range: tuple[float, float] = (0.0, 1.0),
    scale: str = "linear",
    default: float = 0.0,
) -> dict[str, Any]:
    return {
        "animatable": animatable,
        "range": list(range),
        "scale": scale,
        "default": default,
    }


# Dict[effect_type, {"category": str, "params": Dict[param_name, ParamSpec]}]
EFFECT_REGISTRY: dict[str, dict[str, Any]] = {
    # ── Dynamics ────────────────────────────────────────────────────
    "compressor": {
        "category": "dynamics",
        "params": {
            "threshold": _p(animatable=True, range=(-60.0, 0.0), scale="db", default=-24.0),
            "ratio": _p(animatable=True, range=(1.0, 20.0), scale="linear", default=4.0),
            "attack": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.003),
            "release": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.25),
            "knee": _p(animatable=True, range=(0.0, 40.0), scale="linear", default=30.0),
        },
    },
    "gate": {
        "category": "dynamics",
        "params": {
            "threshold": _p(animatable=True, range=(-100.0, 0.0), scale="db", default=-40.0),
            "attack": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.001),
            "release": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.1),
        },
    },
    "limiter": {
        "category": "dynamics",
        "params": {
            "ceiling": _p(animatable=True, range=(-20.0, 0.0), scale="db", default=-0.3),
            "release": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.05),
        },
    },
    # ── EQ ─────────────────────────────────────────────────────────
    "eq_band": {
        "category": "eq",
        "params": {
            "freq": _p(animatable=True, range=(20.0, 20000.0), scale="log", default=1000.0),
            "gain": _p(animatable=True, range=(-24.0, 24.0), scale="db", default=0.0),
            # q: static so users don't accidentally animate it out of useful
            # ranges. R9 exception list doesn't include `q`, but per design
            # review we also keep q static — kept animatable here to match TS.
            "q": _p(animatable=True, range=(0.1, 18.0), scale="linear", default=0.707),
        },
    },
    "highpass": {
        "category": "eq",
        "params": {
            "cutoff": _p(animatable=True, range=(20.0, 20000.0), scale="log", default=80.0),
            "q": _p(animatable=True, range=(0.1, 18.0), scale="linear", default=0.707),
        },
    },
    "lowpass": {
        "category": "eq",
        "params": {
            "cutoff": _p(animatable=True, range=(20.0, 20000.0), scale="log", default=12000.0),
            "q": _p(animatable=True, range=(0.1, 18.0), scale="linear", default=0.707),
        },
    },
    # ── Spatial ────────────────────────────────────────────────────
    "pan": {
        "category": "spatial",
        "params": {
            "pan": _p(animatable=True, range=(-1.0, 1.0), scale="linear", default=0.0),
        },
    },
    "stereo_width": {
        "category": "spatial",
        "params": {
            "width": _p(animatable=True, range=(0.0, 2.0), scale="linear", default=1.0),
        },
    },
    # ── Time-based (sends) ─────────────────────────────────────────
    # R9: `bus_id` is the static routing target — NOT animatable (animating
    # it would mean switching the physical bus every frame).
    "reverb_send": {
        "category": "time",
        "params": {
            "bus_id": _p(animatable=False, range=(0.0, 0.0), scale="linear", default=0.0),
            "level": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.0),
        },
    },
    "delay_send": {
        "category": "time",
        "params": {
            "bus_id": _p(animatable=False, range=(0.0, 0.0), scale="linear", default=0.0),
            "level": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.0),
        },
    },
    "echo_send": {
        "category": "time",
        "params": {
            "bus_id": _p(animatable=False, range=(0.0, 0.0), scale="linear", default=0.0),
            "level": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.0),
        },
    },
    # ── Modulation ─────────────────────────────────────────────────
    # R9: `rate` is the LFO frequency — static per instance.
    "tremolo": {
        "category": "modulation",
        "params": {
            "rate": _p(animatable=False, range=(0.1, 20.0), scale="log", default=4.0),
            "depth": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
        },
    },
    "auto_pan": {
        "category": "modulation",
        "params": {
            "rate": _p(animatable=False, range=(0.1, 20.0), scale="log", default=1.0),
            "depth": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
        },
    },
    "chorus": {
        "category": "modulation",
        "params": {
            "rate": _p(animatable=False, range=(0.1, 20.0), scale="log", default=0.8),
            "depth": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.4),
            "mix": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
        },
    },
    "flanger": {
        "category": "modulation",
        "params": {
            "rate": _p(animatable=False, range=(0.1, 20.0), scale="log", default=0.5),
            "depth": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
            "feedback": _p(animatable=True, range=(0.0, 0.95), scale="linear", default=0.3),
            "mix": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
        },
    },
    "phaser": {
        "category": "modulation",
        "params": {
            "rate": _p(animatable=False, range=(0.1, 20.0), scale="log", default=0.5),
            "depth": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.6),
            "feedback": _p(animatable=True, range=(0.0, 0.95), scale="linear", default=0.3),
            "mix": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
        },
    },
    # ── Distortion ─────────────────────────────────────────────────
    # R9: `character` is the waveshaper curve selector (tube/fuzz/tape) —
    # switching it mid-note would click; static per instance.
    "drive": {
        "category": "distortion",
        "params": {
            "amount": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.3),
            "tone": _p(animatable=True, range=(0.0, 1.0), scale="linear", default=0.5),
            "character": _p(animatable=False, range=(0.0, 0.0), scale="linear", default=0.0),
        },
    },
}


# Synthetic effect_type used ONLY by effect_curves to animate per-bus send levels
# (R8a). Not registered in EFFECT_REGISTRY — POST /track-effects rejects it.
SEND_SYNTHETIC_EFFECT_TYPE = "__send"


def is_valid_effect_type(effect_type: str) -> bool:
    """True if ``effect_type`` is one of the 17 R8 real types.

    Explicitly returns False for ``__send`` (R8a) — that synthetic type is
    valid only as the ``effect_type`` of an ``effect_curves`` row, never a
    ``track_effects`` row.
    """
    return effect_type in EFFECT_REGISTRY


def is_param_animatable(effect_type: str, param_name: str) -> bool | None:
    """Return ``True`` / ``False`` if the param exists on this effect type;
    ``None`` if the effect type OR param is unknown (caller decides how to
    treat the unknown — usually another 400 / 404)."""
    if effect_type == SEND_SYNTHETIC_EFFECT_TYPE:
        # send curves are identified by ``param_name == bus_id`` — always
        # animatable. Actual bus-id existence is checked against the DB.
        return True
    spec = EFFECT_REGISTRY.get(effect_type)
    if spec is None:
        return None
    params = spec["params"]
    if param_name not in params:
        return None
    return bool(params[param_name]["animatable"])


def list_effect_types() -> list[str]:
    """Sorted list of real effect types (excludes __send)."""
    return sorted(EFFECT_REGISTRY.keys())


__all__ = [
    "EFFECT_REGISTRY",
    "SEND_SYNTHETIC_EFFECT_TYPE",
    "is_valid_effect_type",
    "is_param_animatable",
    "list_effect_types",
]
