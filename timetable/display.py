"""Terminal display and table rendering for timetable."""

WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday"
]


def calc_duration(start, end):
    """Calculates duration in minutes between start and end time (HH:MM)."""
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)
    except Exception:
        return 0


def render_day(data, day):
    """Renders schedule for a single day."""
    slots = data.get(day, [])

    day_name = day.capitalize()
    print(f"\n📅 Timetable for {day_name}:")
    print("=" * 65)

    if not slots:
        print("  No classes scheduled.")
        return

    print(f"{'Time':<15} {'Duration':<12} {'Subject':<22} {'Room':<12}")
    print("-" * 65)
    for slot in slots:
        dur = calc_duration(slot.get("start", "00:00"), slot.get("end", "00:00"))
        time_str = f"{slot.get('start')} - {slot.get('end')}"
        dur_str = f"{dur} min"
        print(f"{time_str:<15} {dur_str:<12} {slot.get('subject', ''):<22} {slot.get('room', ''):<12}")


def render_week(data):
    """Renders the entire weekly schedule."""
    print("\n📅 Weekly Timetable Overview")
    print("=" * 65)

    for day in WEEKDAYS:
        slots = data.get(day, [])
        print(f"\n● {day.capitalize()} ({len(slots)} classes):")
        if not slots:
            print("    No classes.")
            continue
        for slot in slots:
            dur = calc_duration(slot.get("start", "00:00"), slot.get("end", "00:00"))
            time_str = f"{slot.get('start')} - {slot.get('end')}"
            print(f"    {time_str:<15} ({dur} min) - {slot.get('subject')} [{slot.get('room')}]")
