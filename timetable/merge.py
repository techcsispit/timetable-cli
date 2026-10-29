"""Merge logic and utilities for combining two timetable JSON files."""

import json
from pathlib import Path
from timetable.display import WEEKDAYS, parse_time
from timetable.conflicts import do_overlap, find_conflicts
from timetable.loader import load_timetable
from timetable.schema import SchemaError
from timetable.diagnostics import Diagnostic, RuleId, Severity, Provenance, AffectedEntry
from timetable.engine import validate_temporal_conflicts


class MergeError(Exception):
    """Raised when an error occurs during timetable reading, validation, or merging."""
    pass


def validate_timetable_structure(data, source_name="timetable"):
    """Validates that ``data`` follows the timetable structure conventions.

    Structure rules:
    - Must be a JSON object (dict).
    - Keys should ideally be string day names.
    - Values associated with day keys must be lists of class dictionaries.
    - Each class dictionary must have subject, start, end, room fields (or valid types).
    """
    if not isinstance(data, dict):
        raise MergeError(
            f"Invalid timetable in '{source_name}': expected a JSON object mapping weekdays to lists of classes."
        )

    target = data
    if "schema_version" in data:
        if "timetable" in data:
            if not isinstance(data["timetable"], dict):
                raise MergeError(
                    f"Invalid timetable in '{source_name}': expected 'timetable' object in versioned timetable."
                )
            target = data["timetable"]
        else:
            target = {k: v for k, v in data.items() if k not in ("schema_version", "metadata")}
    elif "metadata" in data:
        target = {k: v for k, v in data.items() if k != "metadata"}

    for day, slots in target.items():
        if not isinstance(day, str):
            raise MergeError(
                f"Invalid timetable in '{source_name}': weekday keys must be strings."
            )
        if not isinstance(slots, list):
            raise MergeError(
                f"Invalid timetable in '{source_name}': '{day}' must be a list of class slots."
            )
        for idx, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                raise MergeError(
                    f"Invalid timetable in '{source_name}': entry {idx} on '{day}' must be a class object dictionary."
                )


def load_timetable_for_merge(path):
    """Reads and validates a timetable JSON file.

    Raises MergeError if the file is missing, cannot be read, contains invalid JSON,
    or fails structural validation.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise MergeError(f"File not found: '{file_path}'")
    try:
        return load_timetable(file_path)
    except json.JSONDecodeError as exc:
        raise MergeError(f"Invalid JSON in '{file_path}': {exc}") from exc
    except SchemaError as exc:
        raise MergeError(str(exc)) from exc
    except OSError as exc:
        raise MergeError(f"Could not read '{file_path}': {exc}") from exc



def canonicalize_slot(slot):
    """Returns a canonical dictionary representation of a class slot."""
    return {
        "subject": str(slot.get("subject", "")),
        "start": str(slot.get("start", "")),
        "end": str(slot.get("end", "")),
        "room": str(slot.get("room", "")),
    }


def is_duplicate_slot(slot1, slot2):
    """Returns True if two class slots are identical across all relevant class details."""
    return (
        slot1.get("subject") == slot2.get("subject") and
        slot1.get("start") == slot2.get("start") and
        slot1.get("end") == slot2.get("end") and
        slot1.get("room") == slot2.get("room")
    )


def parse_start_minutes(time_str):
    """Parses 'HH:MM' string to total minutes for sorting."""
    try:
        t = parse_time(time_str)
        return t.hour * 60 + t.minute
    except Exception:
        return 0


def merge_timetables(first, second):
    """Combines two timetable dictionaries into a single timetable structure.

    - Preserves all canonical weekdays and any extra keys present.
    - Preserves unique classes.
    - Removes duplicate identical classes.
    - Orders output deterministically by canonical weekday (monday..sunday) then start time.
    - Detects overlapping classes on the same day.

    Returns:
        (merged_data, conflicts)
    """
    # Normalize input keys to lowercase for merging
    first_normalized = {}
    if isinstance(first, dict):
        for k, v in first.items():
            if isinstance(k, str) and isinstance(v, list):
                first_normalized[k.lower()] = v

    second_normalized = {}
    if isinstance(second, dict):
        for k, v in second.items():
            if isinstance(k, str) and isinstance(v, list):
                second_normalized[k.lower()] = v

    # Collect all day keys in canonical order, followed by any custom non-canonical keys
    all_keys = list(WEEKDAYS)
    for k in list(first_normalized.keys()) + list(second_normalized.keys()):
        if k not in all_keys:
            all_keys.append(k)

    merged_data = {}

    for day in all_keys:
        slots_1 = first_normalized.get(day, [])
        slots_2 = second_normalized.get(day, [])

        combined_slots = []

        # Add slots from first input
        for slot in slots_1:
            can_slot = canonicalize_slot(slot)
            if not any(is_duplicate_slot(can_slot, existing) for existing in combined_slots):
                combined_slots.append(can_slot)

        # Add slots from second input if not duplicate
        for slot in slots_2:
            can_slot = canonicalize_slot(slot)
            if not any(is_duplicate_slot(can_slot, existing) for existing in combined_slots):
                combined_slots.append(can_slot)

        # Sort slots deterministically by start time
        combined_slots.sort(key=lambda s: (parse_start_minutes(s["start"]), s["start"], s["subject"]))
        merged_data[day] = combined_slots

    # Detect conflicts in merged dataset
    conflicts = find_conflicts(merged_data)

    return merged_data, conflicts


def render_merge_conflicts(conflicts):
    """Prints merge conflict details to stdout or formatted string."""
    lines = []
    lines.append("⚠️  Merge Conflicts Detected:")
    lines.append("=" * 65)
    for day, a, b in conflicts:
        day_str = day.capitalize()
        lines.append(f"\nConflict on {day_str}:")
        lines.append(f"  • {a.get('start')} - {a.get('end')} : {a.get('subject')} [{a.get('room', 'No Room')}]")
        lines.append(f"  • {b.get('start')} - {b.get('end')} : {b.get('subject')} [{b.get('room', 'No Room')}]")
    lines.append("\n" + "=" * 65)
    return "\n".join(lines)


# --- Three-Way Timetable Merge Implementation ---

class MergeConflict:
    """Represents an unresolvable structural merge conflict between two branches."""

    def __init__(self, conflict_type, day, base_entry=None, ours_entry=None, theirs_entry=None, message=""):
        self.conflict_type = conflict_type
        self.day = day
        self.base_entry = canonicalize_slot(base_entry) if base_entry else None
        self.ours_entry = canonicalize_slot(ours_entry) if ours_entry else None
        self.theirs_entry = canonicalize_slot(theirs_entry) if theirs_entry else None
        self.message = message

    def __repr__(self):
        return f"<MergeConflict {self.conflict_type} on {self.day}: {self.message}>"

    def to_diagnostic(self) -> Diagnostic:
        """Converts this MergeConflict into a structured Diagnostic."""
        affected = []
        if self.base_entry:
            affected.append(AffectedEntry(
                day=self.day,
                slot=self.base_entry,
                provenance=Provenance(source_type="merge", merge_side="base")
            ))
        if self.ours_entry:
            affected.append(AffectedEntry(
                day=self.day,
                slot=self.ours_entry,
                provenance=Provenance(source_type="merge", merge_side="ours")
            ))
        if self.theirs_entry:
            affected.append(AffectedEntry(
                day=self.day,
                slot=self.theirs_entry,
                provenance=Provenance(source_type="merge", merge_side="theirs")
            ))
        return Diagnostic(
            rule_id=RuleId.MERGE_STRUCTURAL_CONFLICT,
            severity=Severity.ERROR,
            message=self.message or f"Structural merge conflict ({self.conflict_type}) on {self.day.capitalize()}",
            affected_entries=affected,
            source=Provenance(source_type="merge"),
            context={"conflict_type": self.conflict_type, "day": self.day}
        )

    def sort_key(self):
        day_idx = WEEKDAYS.index(self.day.lower()) if self.day.lower() in WEEKDAYS else len(WEEKDAYS)
        start = ""
        subject = ""
        for e in (self.ours_entry, self.theirs_entry, self.base_entry):
            if e:
                start = e.get("start", "")
                subject = e.get("subject", "")
                break
        return (day_idx, self.day.lower(), start, subject, self.conflict_type)


class ThreeWayMergeResult:
    """Holds the result of a three-way timetable merge."""

    def __init__(self, merged_data, merge_conflicts=None, schedule_conflicts=None, validation_errors=None, diagnostics=None):
        self.merged_data = merged_data
        self.merge_conflicts = sorted(merge_conflicts or [], key=lambda c: c.sort_key())
        self.schedule_conflicts = schedule_conflicts or []
        self.validation_errors = validation_errors or []
        if diagnostics is not None:
            self.diagnostics = sorted(diagnostics, key=lambda d: d.sort_key())
        else:
            diags = [c.to_diagnostic() for c in self.merge_conflicts]
            for day, a, b in self.schedule_conflicts:
                diags.append(Diagnostic(
                    rule_id=RuleId.MERGE_SCHEDULE_CONFLICT,
                    severity=Severity.ERROR,
                    message=f"Schedule conflict in merged timetable on {day.capitalize()}: '{a.get('subject')}' ({a.get('start')}-{a.get('end')}) overlaps with '{b.get('subject')}' ({b.get('start')}-{b.get('end')})",
                    affected_entries=[
                        AffectedEntry(day=day, slot=a, provenance=Provenance(source_type="merge")),
                        AffectedEntry(day=day, slot=b, provenance=Provenance(source_type="merge")),
                    ],
                    source=Provenance(source_type="merge"),
                    context={"day": day, "slot_a": a, "slot_b": b}
                ))
            for err in self.validation_errors:
                diags.append(Diagnostic(
                    rule_id=RuleId.ENTRY_DUPLICATE if "Duplicate" in err else RuleId.ENTRY_MALFORMED,
                    severity=Severity.ERROR,
                    message=err,
                    source=Provenance(source_type="merge")
                ))
            self.diagnostics = sorted(diags, key=lambda d: d.sort_key())

    @property
    def has_conflicts(self):
        """True if there are merge conflicts, schedule conflicts, or validation errors."""
        return bool(self.merge_conflicts or self.schedule_conflicts or self.validation_errors or any(d.is_error for d in self.diagnostics))

    @property
    def is_clean(self):
        """True if merge succeeded without any merge conflicts, schedule conflicts, or validation errors."""
        return not self.has_conflicts


def _extract_day_slots(data):
    """Normalizes input data and returns a mapping from lowercase weekday to list of canonical slots."""
    if not isinstance(data, dict):
        return {day: [] for day in WEEKDAYS}
    target = data
    if "schema_version" in data:
        if "timetable" in data and isinstance(data["timetable"], dict):
            target = data["timetable"]
        else:
            target = {k: v for k, v in data.items() if k not in ("schema_version", "metadata")}
    elif "metadata" in data:
        target = {k: v for k, v in data.items() if k != "metadata"}

    res = {}
    for k, v in target.items():
        if isinstance(k, str) and isinstance(v, list):
            res[k.lower()] = [canonicalize_slot(s) for s in v if isinstance(s, dict)]
    return res


def _match_slots(base_slots, branch_slots, day=""):
    """Matches base slots with branch slots to identify unchanged, modified, removed, and added entries.

    Returns:
        matched_map: dict mapping base_idx -> (branch_idx or None)
        unmatched_branch_idxs: set of branch_idxs not matched to any base slot
        ambiguous_conflicts: list of MergeConflict if matching is ambiguous
    """
    matched_base = {}
    used_branch = set()
    ambiguous_conflicts = []

    # 1. Exact matches (all 4 fields identical)
    for b_idx, b_slot in enumerate(base_slots):
        exact_matches = [
            br_idx for br_idx, br_slot in enumerate(branch_slots)
            if br_idx not in used_branch and is_duplicate_slot(b_slot, br_slot)
        ]
        if exact_matches:
            chosen = exact_matches[0]
            matched_base[b_idx] = chosen
            used_branch.add(chosen)

    # 2. Same subject matches for remaining slots
    remaining_base = [i for i in range(len(base_slots)) if i not in matched_base]
    for b_idx in remaining_base:
        b_slot = base_slots[b_idx]
        subj = b_slot["subject"]
        subj_candidates = [
            br_idx for br_idx, br_slot in enumerate(branch_slots)
            if br_idx not in used_branch and br_slot["subject"] == subj
        ]
        if len(subj_candidates) == 1:
            chosen = subj_candidates[0]
            matched_base[b_idx] = chosen
            used_branch.add(chosen)
        elif len(subj_candidates) > 1:
            # Score candidates by number of matching fields
            def score(cand_idx):
                cand = branch_slots[cand_idx]
                return sum(1 for f in ("start", "end", "room") if cand[f] == b_slot[f])
            scores = [score(c) for c in subj_candidates]
            max_score = max(scores)
            best_cands = [c for c, s in zip(subj_candidates, scores) if s == max_score]
            if len(best_cands) == 1:
                chosen = best_cands[0]
                matched_base[b_idx] = chosen
                used_branch.add(chosen)
            else:
                ambiguous_conflicts.append(
                    MergeConflict(
                        "ambiguous_match",
                        day,
                        base_entry=b_slot,
                        message=f"Ambiguous match for base class '{subj}' on {day.capitalize()}"
                    )
                )

    # 3. Same clock slot and room matches for remaining (rename in place)
    remaining_base = [i for i in range(len(base_slots)) if i not in matched_base]
    for b_idx in remaining_base:
        b_slot = base_slots[b_idx]
        slot_candidates = [
            br_idx for br_idx, br_slot in enumerate(branch_slots)
            if br_idx not in used_branch
            and br_slot["start"] == b_slot["start"]
            and br_slot["end"] == b_slot["end"]
            and br_slot["room"] == b_slot["room"]
        ]
        if len(slot_candidates) == 1:
            chosen = slot_candidates[0]
            matched_base[b_idx] = chosen
            used_branch.add(chosen)
        elif len(slot_candidates) > 1:
            ambiguous_conflicts.append(
                MergeConflict(
                    "ambiguous_match",
                    day,
                    base_entry=b_slot,
                    message=f"Ambiguous in-place rename match on {day.capitalize()} for slot {b_slot['start']}-{b_slot['end']} in {b_slot['room']}"
                )
            )

    for b_idx in range(len(base_slots)):
        if b_idx not in matched_base:
            matched_base[b_idx] = None

    unmatched_branch = [i for i in range(len(branch_slots)) if i not in used_branch]
    return matched_base, unmatched_branch, ambiguous_conflicts


def _reconcile_entry(day, base_slot, ours_slot, theirs_slot):
    """Reconciles a single base entry given its counterparts in ours and theirs.

    Returns:
        (resolved_slot, conflict_or_none)
    """
    # Neither changed
    if ours_slot == base_slot and theirs_slot == base_slot:
        return base_slot, None

    # Only ours deleted
    if ours_slot is None and theirs_slot == base_slot:
        return None, None

    # Only theirs deleted
    if theirs_slot is None and ours_slot == base_slot:
        return None, None

    # Both deleted
    if ours_slot is None and theirs_slot is None:
        return None, None

    # Delete vs modify
    if ours_slot is None and theirs_slot is not None:
        if theirs_slot != base_slot:
            return None, MergeConflict(
                "delete_vs_modify",
                day,
                base_entry=base_slot,
                theirs_entry=theirs_slot,
                message=f"Delete-vs-modify conflict on {day.capitalize()}: deleted in ours, modified in theirs ('{theirs_slot['subject']}')"
            )
        return None, None

    if theirs_slot is None and ours_slot is not None:
        if ours_slot != base_slot:
            return None, MergeConflict(
                "delete_vs_modify",
                day,
                base_entry=base_slot,
                ours_entry=ours_slot,
                message=f"Delete-vs-modify conflict on {day.capitalize()}: modified in ours ('{ours_slot['subject']}'), deleted in theirs"
            )
        return None, None

    # Both kept and possibly modified
    if ours_slot == base_slot:
        # Only theirs changed
        return canonicalize_slot(theirs_slot), None

    if theirs_slot == base_slot:
        # Only ours changed
        return canonicalize_slot(ours_slot), None

    # Both sides changed: check if identical
    if is_duplicate_slot(ours_slot, theirs_slot):
        return canonicalize_slot(ours_slot), None

    # Field-level reconciliation
    merged = {}
    conflict_fields = []
    for f in ("subject", "start", "end", "room"):
        b_val = base_slot[f]
        o_val = ours_slot[f]
        t_val = theirs_slot[f]
        if o_val == b_val and t_val == b_val:
            merged[f] = b_val
        elif o_val != b_val and t_val == b_val:
            merged[f] = o_val
        elif t_val != b_val and o_val == b_val:
            merged[f] = t_val
        elif o_val == t_val:
            merged[f] = o_val
        else:
            conflict_fields.append(f)

    if conflict_fields:
        return None, MergeConflict(
            "modify_vs_modify",
            day,
            base_entry=base_slot,
            ours_entry=ours_slot,
            theirs_entry=theirs_slot,
            message=f"Conflicting modifications on {day.capitalize()} for '{base_slot['subject']}' in fields: {', '.join(conflict_fields)}"
        )

    return canonicalize_slot(merged), None


def merge_three_way(base, ours, theirs):
    """Performs a three-way merge among base, ours, and theirs timetables.

    Reconciles changes relative to base according to RFC-standard three-way merge
    semantics, detects structural merge conflicts, and validates the resulting candidate
    schedule for schedule conflicts (overlaps) and schema validity.

    Returns:
        ThreeWayMergeResult
    """
    base_dict = _extract_day_slots(base)
    ours_dict = _extract_day_slots(ours)
    theirs_dict = _extract_day_slots(theirs)

    all_days = list(WEEKDAYS)
    for d in list(base_dict.keys()) + list(ours_dict.keys()) + list(theirs_dict.keys()):
        if d not in all_days:
            all_days.append(d)

    merged_data = {}
    merge_conflicts = []

    for day in all_days:
        base_slots = base_dict.get(day, [])
        ours_slots = ours_dict.get(day, [])
        theirs_slots = theirs_dict.get(day, [])

        ours_matched, ours_unmatched_idxs, ours_ambig = _match_slots(base_slots, ours_slots, day=day)
        theirs_matched, theirs_unmatched_idxs, theirs_ambig = _match_slots(base_slots, theirs_slots, day=day)

        merge_conflicts.extend(ours_ambig)
        merge_conflicts.extend(theirs_ambig)

        day_resolved_slots = []

        # 1. Reconcile entries present in base
        for b_idx, base_slot in enumerate(base_slots):
            o_idx = ours_matched.get(b_idx)
            t_idx = theirs_matched.get(b_idx)
            o_slot = ours_slots[o_idx] if o_idx is not None else None
            t_slot = theirs_slots[t_idx] if t_idx is not None else None

            resolved, conflict = _reconcile_entry(day, base_slot, o_slot, t_slot)
            if conflict:
                merge_conflicts.append(conflict)
            elif resolved is not None:
                day_resolved_slots.append(resolved)

        # 2. Reconcile independent additions
        ours_additions = [ours_slots[i] for i in ours_unmatched_idxs]
        theirs_additions = [theirs_slots[i] for i in theirs_unmatched_idxs]

        used_theirs_adds = set()
        for o_add in ours_additions:
            # Check if there is an identical addition in theirs
            ident_match = next(
                (j for j, t_add in enumerate(theirs_additions) if j not in used_theirs_adds and is_duplicate_slot(o_add, t_add)),
                None
            )
            if ident_match is not None:
                day_resolved_slots.append(o_add)
                used_theirs_adds.add(ident_match)
            else:
                # Check for competing addition with same subject or same clock slot
                same_subj = next(
                    (j for j, t_add in enumerate(theirs_additions) if j not in used_theirs_adds and t_add["subject"] == o_add["subject"]),
                    None
                )
                same_slot = next(
                    (j for j, t_add in enumerate(theirs_additions) if j not in used_theirs_adds and t_add["start"] == o_add["start"] and t_add["end"] == o_add["end"] and t_add["room"] == o_add["room"]),
                    None
                )
                if same_subj is not None:
                    t_add = theirs_additions[same_subj]
                    merge_conflicts.append(
                        MergeConflict(
                            "add_vs_add",
                            day,
                            ours_entry=o_add,
                            theirs_entry=t_add,
                            message=f"Conflicting independent additions of subject '{o_add['subject']}' on {day.capitalize()}"
                        )
                    )
                    used_theirs_adds.add(same_subj)
                elif same_slot is not None:
                    t_add = theirs_additions[same_slot]
                    merge_conflicts.append(
                        MergeConflict(
                            "add_vs_add",
                            day,
                            ours_entry=o_add,
                            theirs_entry=t_add,
                            message=f"Conflicting independent additions on {day.capitalize()} for same slot {o_add['start']}-{o_add['end']} in {o_add['room']}"
                        )
                    )
                    used_theirs_adds.add(same_slot)
                else:
                    # Distinct addition
                    day_resolved_slots.append(o_add)

        for j, t_add in enumerate(theirs_additions):
            if j not in used_theirs_adds:
                day_resolved_slots.append(t_add)

        # Sort slots deterministically by start time, end time, and subject
        day_resolved_slots.sort(key=lambda s: (parse_start_minutes(s["start"]), s["start"], s["end"], s["subject"]))
        merged_data[day] = day_resolved_slots

    # Validate resulting schedule for scheduling conflicts and schema validation
    schedule_conflicts = find_conflicts(merged_data)
    validation_errors = []

    # Check for duplicate entries on same day (if any slipped through)
    for day, slots in merged_data.items():
        seen = []
        for s in slots:
            if any(is_duplicate_slot(s, prev) for prev in seen):
                validation_errors.append(f"Duplicate entry on {day.capitalize()}: {s['subject']} ({s['start']}-{s['end']})")
            seen.append(s)

    return ThreeWayMergeResult(
        merged_data,
        merge_conflicts=merge_conflicts,
        schedule_conflicts=schedule_conflicts,
        validation_errors=validation_errors,
    )


def render_three_way_conflicts(result):
    """Renders structured diagnostics for merge and schedule conflicts to formatted string."""
    lines = []
    if result.merge_conflicts:
        lines.append("❌  Merge Conflicts (Incompatible Branch Changes):")
        lines.append("=" * 65)
        for c in result.merge_conflicts:
            day_str = c.day.capitalize()
            lines.append(f"\nConflict on {day_str} [{c.conflict_type}]:")
            lines.append(f"  {c.message}")
            if c.base_entry:
                b = c.base_entry
                lines.append(f"  • Base:   {b['start']}-{b['end']} : {b['subject']} [{b['room']}]")
            if c.ours_entry:
                o = c.ours_entry
                lines.append(f"  • Ours:   {o['start']}-{o['end']} : {o['subject']} [{o['room']}]")
            if c.theirs_entry:
                t = c.theirs_entry
                lines.append(f"  • Theirs: {t['start']}-{t['end']} : {t['subject']} [{t['room']}]")
        lines.append("\n" + "=" * 65)

    if result.schedule_conflicts:
        if lines:
            lines.append("")
        lines.append("⚠️  Schedule Conflicts (Overlapping Classes in Merged Result):")
        lines.append("=" * 65)
        for day, a, b in result.schedule_conflicts:
            day_str = day.capitalize()
            lines.append(f"\nConflict on {day_str}:")
            lines.append(f"  • {a.get('start')} - {a.get('end')} : {a.get('subject')} [{a.get('room', 'No Room')}]")
            lines.append(f"  • {b.get('start')} - {b.get('end')} : {b.get('subject')} [{b.get('room', 'No Room')}]")
        lines.append("\n" + "=" * 65)

    if result.validation_errors:
        if lines:
            lines.append("")
        lines.append("⚠️  Validation Errors in Merged Result:")
        lines.append("=" * 65)
        for err in result.validation_errors:
            lines.append(f"  • {err}")
        lines.append("=" * 65)

    return "\n".join(lines)
