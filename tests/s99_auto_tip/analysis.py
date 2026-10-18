"""Feature summaries and conservative range-overlap analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .trace import validate_trace


KNOWN_PROFILES = ("c245_2p5_ohm", "c245_5p5_ohm")


def _empty_features(trace: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "trace_id": trace["trace_id"],
        "trace_kind": trace["trace_kind"],
        "provenance": trace["provenance"],
        "profile": trace["metadata"].get("cartridge", {}).get("profile", "unknown"),
        "supply_kind": trace["metadata"].get("supply", {}).get("kind", "unknown"),
        "sample_count": len(trace["samples"]),
        "valid_temperature_sample_count": 0,
        "duration_us": trace["samples"][-1]["time_us"] - trace["samples"][0]["time_us"],
        "temperature_rise_c": None,
        "maximum_temperature_rise_c": None,
        "feature_status": status,
    }


def trace_features(trace: dict[str, Any]) -> dict[str, Any]:
    """Return simple observed thermal features without assigning a tip class."""
    validate_trace(trace)
    if trace["provenance"] == "measured_hardware":
        if trace["metadata"].get("environment", {}).get("no_contact_verified") is not True:
            return _empty_features(trace, "unknown_no_contact_state_not_verified")
        if any(sample.get("source_state") != "stable" for sample in trace["samples"]):
            return _empty_features(trace, "unknown_source_state_not_stable_or_missing")
        if any(sample.get("tip_temperature_valid") is not True for sample in trace["samples"]):
            return _empty_features(trace, "unknown_invalid_or_missing_temperature_sample")
        invalid_event_words = (
            "fault",
            "trip",
            "detach",
            "reset",
            "interlock",
            "estop",
            "watchdog",
        )
        has_invalid_event = any(
            any(word in event["type"].lower() for word in invalid_event_words)
            for event in trace["events"]
        )
        if has_invalid_event:
            return _empty_features(trace, "unknown_fault_or_interlock_event")
    valid = [
        sample
        for sample in trace["samples"]
        if sample.get("tip_temperature_valid") is True
        and sample.get("tip_temperature_c") is not None
    ]
    result: dict[str, Any] = {
        "trace_id": trace["trace_id"],
        "trace_kind": trace["trace_kind"],
        "provenance": trace["provenance"],
        "profile": trace["metadata"].get("cartridge", {}).get("profile", "unknown"),
        "supply_kind": trace["metadata"].get("supply", {}).get("kind", "unknown"),
        "sample_count": len(trace["samples"]),
        "valid_temperature_sample_count": len(valid),
        "duration_us": trace["samples"][-1]["time_us"] - trace["samples"][0]["time_us"],
        "temperature_rise_c": None,
        "maximum_temperature_rise_c": None,
    }
    if len(valid) < 2:
        return _empty_features(trace, "unknown_insufficient_valid_temperature_samples")
    initial = valid[0]["tip_temperature_c"]
    temperatures = [sample["tip_temperature_c"] for sample in valid]
    result["temperature_rise_c"] = temperatures[-1] - initial
    result["maximum_temperature_rise_c"] = max(temperatures) - initial
    result["feature_status"] = "observed"
    return result


def compare_observed_ranges(
    traces: Iterable[dict[str, Any]], feature: str = "temperature_rise_c"
) -> dict[str, Any]:
    """Report empirical feature overlap; this deliberately is not a classifier.

    Even disjoint measured sample ranges are not a qualified threshold. Synthetic
    data is always treated as unknown evidence for real-tip discrimination.
    """
    traces = list(traces)
    features = [trace_features(trace) for trace in traces]
    grouped: dict[str, list[float]] = defaultdict(list)
    known_provenance: list[str] = []
    for item in features:
        if item["profile"] in KNOWN_PROFILES and item.get(feature) is not None:
            grouped[item["profile"]].append(float(item[feature]))
            known_provenance.append(item["provenance"])

    ranges: dict[str, dict[str, Any]] = {}
    for profile in KNOWN_PROFILES:
        values = grouped.get(profile, [])
        ranges[profile] = {
            "count": len(values),
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
        }

    have_both = all(ranges[profile]["count"] > 0 for profile in KNOWN_PROFILES)
    invalid_known_profile_traces = [
        item["trace_id"]
        for item in features
        if item["profile"] in KNOWN_PROFILES and item.get(feature) is None
    ]
    overlap: bool | None = None
    if have_both:
        low_a, high_a = ranges[KNOWN_PROFILES[0]]["minimum"], ranges[KNOWN_PROFILES[0]]["maximum"]
        low_b, high_b = ranges[KNOWN_PROFILES[1]]["minimum"], ranges[KNOWN_PROFILES[1]]["maximum"]
        overlap = low_a <= high_b and low_b <= high_a

    if invalid_known_profile_traces:
        result = "unknown"
        reason = "one_or_more_known_profile_traces_are_invalid_or_incomplete"
    elif not have_both:
        result = "unknown"
        reason = "both_known_profile_groups_are_required"
    elif overlap:
        result = "unknown"
        reason = "observed_feature_ranges_overlap"
    elif any(provenance != "measured_hardware" for provenance in known_provenance):
        result = "unknown"
        reason = "synthetic_or_mixed_data_cannot_establish_hardware_separation"
    else:
        result = "separated_in_observed_samples_unqualified"
        reason = "observed_ranges_do_not_overlap_but_no_classifier_threshold_is_qualified"

    return {
        "feature": feature,
        "class_ranges": ranges,
        "observed_ranges_overlap": overlap,
        "invalid_known_profile_trace_ids": invalid_known_profile_traces,
        "result": result,
        "reason": reason,
        "classifier_threshold": None,
        "qualification": "none; this report is descriptive and does not classify a cartridge",
        "traces": features,
    }
