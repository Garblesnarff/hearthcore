"""Phase II Bounded Playstyle Traits & Traceable Scoring Adapter (Card II-09).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 8):
- 8 canonical traits on [0.0, 1.0] with neutral default 0.5 (full backwards compatibility with
  existing characters/<slug>/profile.json files that omit `traits`).
- Operates ONLY on discretionary candidate scoring with bounded +/-6 point impact and explicit
  per-candidate adjustment trace.
- NEVER overrides mandatory safety, repair, raid readiness, role feasibility, or owner commands.
- NEVER double-counts existing Memory v1 variety fatigue or scoped stall caution.
"""
from __future__ import annotations

from typing import Any

TRAIT_NAMES = (
    "persistence",
    "preparation",
    "sociability",
    "curiosity",
    "frugality",
    "variety_seeking",
    "generosity",
    "risk_tolerance",
)
NEUTRAL_TRAIT = 0.5
MAX_DISCRETIONARY_DELTA = 6


def normalize_traits(char_profile: dict[str, Any]) -> dict[str, float]:
    """Extract canonical bounded [0.0, 1.0] playstyle traits from a profile (neutral 0.5 default)."""
    raw = char_profile.get("traits") if isinstance(char_profile.get("traits"), dict) else {}
    out: dict[str, float] = {}
    for name in TRAIT_NAMES:
        val = raw.get(name, NEUTRAL_TRAIT)
        try:
            f = float(val)
        except (TypeError, ValueError):
            f = NEUTRAL_TRAIT
        out[name] = round(max(0.0, min(1.0, f)), 3)
    return out


def apply_playstyle_scoring(
    candidates: list[dict[str, Any]],
    char_profile: dict[str, Any],
    *,
    has_fatigue_nudge: bool = False,
    has_stall_caution: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply bounded, traceable discretionary trait adjustments without stacking memory nudges."""
    traits = normalize_traits(char_profile)
    adjusted: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []

    for c in candidates:
        item = dict(c)
        ctype = str(item.get("type") or "")
        delta = 0.0
        reasons: list[str] = []

        # Sociability gently nudges voluntary group opportunities (never forces)
        if ctype == "dungeon":
            d = (traits["sociability"] - 0.5) * 8.0 + (traits["risk_tolerance"] - 0.5) * 4.0
            if abs(d) >= 0.5:
                delta += d
                reasons.append(f"sociability/risk:{d:+.1f}")
        # Generosity nudges guild crafting orders / guild vault gathering
        elif ctype == "guild_order":
            d = (traits["generosity"] - 0.5) * 8.0
            if abs(d) >= 0.5:
                delta += d
                reasons.append(f"generosity:{d:+.1f}")
        # Preparation nudges training / repair / gathering supplies
        elif ctype in ("train", "repair"):
            d = (traits["preparation"] - 0.5) * 6.0
            if abs(d) >= 0.5:
                delta += d
                reasons.append(f"preparation:{d:+.1f}")
        # Curiosity nudges exploration / frontier destinations
        elif ctype == "explore":
            d = (traits["curiosity"] - 0.5) * 8.0
            if abs(d) >= 0.5:
                delta += d
                reasons.append(f"curiosity:{d:+.1f}")
        # Frugality nudges gathering/fishing/banking over spending
        elif ctype in ("gather", "fish", "bank"):
            d = (traits["frugality"] - 0.5) * 6.0
            if abs(d) >= 0.5:
                delta += d
                reasons.append(f"frugality:{d:+.1f}")

        # Avoid stacking variety_seeking on top of existing Memory v1 fatigue/stall adjustments!
        if not has_fatigue_nudge and not has_stall_caution and ctype in ("quest", "craft", "gather"):
            d_var = (traits["variety_seeking"] - 0.5) * 2.0
            if abs(d_var) >= 0.5:
                delta += d_var
                reasons.append(f"variety:{d_var:+.1f}")

        bounded = int(round(max(-MAX_DISCRETIONARY_DELTA, min(MAX_DISCRETIONARY_DELTA, delta))))
        if bounded != 0:
            item["score"] = max(1, min(100, int(item["score"]) + bounded))
            trace.append({
                "candidate_id": item["id"],
                "delta": bounded,
                "reasons": reasons,
            })
        adjusted.append(item)

    return adjusted, trace
