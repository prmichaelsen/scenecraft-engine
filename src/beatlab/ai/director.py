"""AI director — orchestrates LLM call to generate effect plans."""

from __future__ import annotations

import json
import sys
from datetime import datetime

from beatlab.ai.plan import EffectPlan, SectionPlan, parse_effect_plan, validate_effect_plan
from beatlab.ai.prompt import build_system_prompt, build_user_prompt
from beatlab.ai.provider import LLMProvider


BATCH_SIZE = 25  # Max sections per LLM call
CONTEXT_OVERLAP = 3  # How many previous sections to include as context


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def create_effect_plan(
    beat_map: dict,
    provider: LLMProvider,
    user_prompt: str | None = None,
    audio_descriptions: list[str] | None = None,
) -> EffectPlan:
    """Send section data to LLM and get back a validated effect plan.

    For tracks with many sections (>BATCH_SIZE), splits into batches
    and includes previous batch context for continuity.

    Args:
        beat_map: Parsed beat map dict with sections.
        provider: LLM provider to call.
        user_prompt: Optional freeform creative direction from user.
        audio_descriptions: Optional per-section text descriptions from audio model.

    Returns:
        Validated EffectPlan.
    """
    sections = beat_map.get("sections", [])

    if len(sections) <= BATCH_SIZE:
        return _plan_single_batch(beat_map, provider, user_prompt, audio_descriptions)

    return _plan_batched(beat_map, provider, user_prompt, audio_descriptions)


def _plan_single_batch(
    beat_map: dict,
    provider: LLMProvider,
    user_prompt: str | None,
    audio_descriptions: list[str] | None,
) -> EffectPlan:
    """Plan all sections in one LLM call."""
    system = build_system_prompt()
    user = build_user_prompt(beat_map, user_prompt=user_prompt, audio_descriptions=audio_descriptions)

    response_text = provider.complete(system, user)
    plan = parse_effect_plan(response_text)

    warnings = validate_effect_plan(plan)
    for w in warnings:
        _log(f"  Warning: {w}")

    return plan


def _plan_batched(
    beat_map: dict,
    provider: LLMProvider,
    user_prompt: str | None,
    audio_descriptions: list[str] | None,
) -> EffectPlan:
    """Plan sections in batches with context overlap for continuity."""
    sections = beat_map.get("sections", [])
    total = len(sections)
    num_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    _log(f"  Batched planning: {total} sections in {num_batches} batches of ~{BATCH_SIZE}")

    all_section_plans: list[SectionPlan] = []
    previous_plans: list[dict] = []  # Last few plans for context

    for batch_idx in range(num_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, total)
        batch_sections = sections[start:end]

        _log(f"  Batch {batch_idx + 1}/{num_batches}: sections {start}-{end - 1}")

        # Build a mini beat_map for this batch
        batch_beat_map = dict(beat_map)
        batch_beat_map["sections"] = batch_sections

        # Filter beats to this batch's time range
        batch_start_time = batch_sections[0].get("start_time", 0)
        batch_end_time = batch_sections[-1].get("end_time", 0)
        batch_beat_map["beats"] = [
            b for b in beat_map.get("beats", [])
            if batch_start_time <= b.get("time", 0) < batch_end_time
        ]

        # Slice audio descriptions for this batch
        batch_descriptions = None
        if audio_descriptions:
            batch_descriptions = audio_descriptions[start:end]

        # Build the prompt with context from previous batch
        system = build_system_prompt()
        user = build_user_prompt(
            batch_beat_map,
            user_prompt=user_prompt,
            audio_descriptions=batch_descriptions,
        )

        # Add previous batch context
        if previous_plans:
            context_str = "\n## Previous Sections (for continuity — DO NOT re-plan these)\n\n"
            context_str += "Here's what was planned for the sections immediately before this batch. "
            context_str += "Use this to maintain visual continuity and narrative arc:\n\n"
            for pp in previous_plans[-CONTEXT_OVERLAP:]:
                context_str += f"- Section {pp['section_index']}: style=\"{pp.get('style_prompt', '?')}\", "
                context_str += f"transition=\"{pp.get('transition_action', 'none')}\"\n"
            context_str += "\nNow plan the following sections (starting at section {}):\n\n".format(start)
            user = context_str + user

        # Remap section indices — tell Claude the real indices
        user = user.replace("### Section 0 ", f"### Section {start} ")
        for j in range(len(batch_sections)):
            user = user.replace(f"### Section {j} (", f"### Section {start + j} (")

        response_text = provider.complete(system, user)
        batch_plan = parse_effect_plan(response_text)

        # Remap parsed section indices back to global
        for sp in batch_plan.sections:
            # The LLM might return 0-indexed or global — handle both
            if sp.section_index < start:
                sp.section_index += start

        warnings = validate_effect_plan(batch_plan)
        for w in warnings:
            _log(f"  Warning: {w}")

        all_section_plans.extend(batch_plan.sections)

        # Save last few for context
        for sp in batch_plan.sections:
            previous_plans.append({
                "section_index": sp.section_index,
                "style_prompt": sp.style_prompt,
                "transition_action": sp.transition_action,
                "presets": sp.presets,
                "intensity_curve": sp.intensity_curve,
            })

        _log(f"  Batch {batch_idx + 1}/{num_batches}: {len(batch_plan.sections)} sections planned")

    _log(f"  Batched planning complete: {len(all_section_plans)} total sections")
    return EffectPlan(sections=all_section_plans)
