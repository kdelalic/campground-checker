import calendar
import html
from collections import defaultdict
from datetime import date, datetime, timezone

from camply.containers import AvailableCampsite

from .results import ProcessedAvailability, process_results


def get_dashboard_path(args, config: dict) -> str | None:
    """Resolve dashboard output path.

    Priority: --no-dashboard > --dashboard CLI arg > dashboard.output_path in YAML > None.
    """
    if getattr(args, "no_dashboard", False):
        return None
    cli_path = getattr(args, "dashboard", None)
    if cli_path is not None:
        return cli_path
    dash_cfg = config.get("dashboard") or {}
    return dash_cfg.get("output_path")


def build_calendar_html(all_availabilities: dict[date, int]) -> str:
    if not all_availabilities:
        return ""

    min_date = min(all_availabilities.keys())
    max_date = max(all_availabilities.keys())

    cal = calendar.Calendar(firstweekday=6)  # Sunday start

    months_html = []
    month_options = []
    curr_year = min_date.year
    curr_month = min_date.month

    month_idx = 0
    while (curr_year, curr_month) <= (max_date.year, max_date.month):
        month_name = calendar.month_name[curr_month]
        month_id = f"cal-month-{month_idx}"
        month_label = f"{month_name} {curr_year}"

        month_options.append(f'<option value="{month_id}">{month_label}</option>')

        weeks = cal.monthdatescalendar(curr_year, curr_month)

        rows = []
        for week in weeks:
            cells = []
            for d in week:
                if d.month != curr_month:
                    cells.append('<td class="calendar-empty"></td>')
                else:
                    count = all_availabilities.get(d, 0)
                    if count > 0:
                        cells.append(
                            f'<td class="calendar-available" data-date="{d.isoformat()}" title="{count} site(s) available">{d.day}</td>'
                        )
                    else:
                        cells.append(f'<td class="calendar-day">{d.day}</td>')
            rows.append(f"<tr>{''.join(cells)}</tr>")

        display_style = 'style="display: none;"' if month_idx > 0 else ""
        month_html = (
            f'<div class="calendar-month" id="{month_id}" {display_style}>'
            f'<table class="calendar-table">'
            f"<thead><tr><th>Su</th><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            f"</table>"
            f"</div>"
        )
        months_html.append(month_html)

        curr_month += 1
        if curr_month > 12:
            curr_month = 1
            curr_year += 1
        month_idx += 1

    header_html = (
        f'<div class="calendar-controls">'
        f'<button id="prev-month-btn" class="nav-btn" title="Previous Month" disabled>&larr;</button>'
        f'<select id="month-selector">{"".join(month_options)}</select>'
        f'<button id="next-month-btn" class="nav-btn" title="Next Month" disabled>&rarr;</button>'
        f'<button id="clear-date-filter" style="display: none;">Clear Filter</button>'
        f"</div>"
    )

    return f'<div class="calendar-container">{header_html}{"".join(months_html)}</div>\n'


def build_dashboard_html(
    entries_with_results: list[tuple[dict, list[AvailableCampsite]] | ProcessedAvailability],
    day_filter: set[int] | None,
    scan_timestamp: datetime | None = None,
) -> str:
    """Generate a complete self-contained HTML string."""
    if scan_timestamp is None:
        scan_timestamp = datetime.now(timezone.utc).astimezone()

    cards_html = []
    nav_links = []
    all_availabilities = defaultdict(int)

    availabilities = [
        item
        if isinstance(item, ProcessedAvailability)
        else process_results(item[0], item[1], day_filter)
        for item in entries_with_results
    ]

    for availability in availabilities:
        entry = availability.entry
        # Resolve the display name: use camply result metadata when available,
        # otherwise fall back to the entry's config-level name.
        if availability.available:
            name = availability.facility_name
        else:
            name = entry.get("name") or f"Campground #{entry.get('campground_id', '?')}"

        safe_name = html.escape(name)
        card_id = f"site-{len(cards_html)}"

        if not availability.available:
            # No availability for this campground — show a muted card.
            nav_links.append(
                f'<li data-ref="{card_id}" data-unavailable="true">'
                f'<a href="#{card_id}">{safe_name}</a> '
                f'<span class="nav-count nav-none">\u2014</span></li>'
            )
            cards_html.append(
                f'<div class="card card-unavailable" id="{card_id}">'
                f'<div class="card-header">'
                f"<h2>{safe_name}</h2>"
                f'<span class="site-count site-count-none">No availability</span>'
                f"</div>"
                f"</div>"
            )
            continue

        by_date = availability.campsite_ids_by_date
        for booking_date, campsite_ids in by_date.items():
            all_availabilities[booking_date] += len(campsite_ids)
        total = availability.total_sites

        nav_links.append(
            f'<li data-ref="{card_id}"><a href="#{card_id}">{safe_name}</a> <span class="nav-count">{total}</span></li>'
        )

        rows_html = []
        for d in sorted(by_date):
            count = len(by_date[d])
            date_str = html.escape(d.strftime("%a, %b %-d"))
            rows_html.append(
                f'<tr data-date="{d.isoformat()}" data-count="{count}">'
                f"<td>{date_str}</td>"
                f'<td><span class="available-badge">{count} site(s)</span></td>'
                f"</tr>"
            )

        book_link = ""
        if availability.booking_url:
            safe_url = html.escape(availability.booking_url)
            book_link = f'<div class="book-action"><a class="book-link" href="{safe_url}" target="_blank">Book now &rarr;</a></div>'

        cards_html.append(
            f'<div class="card" id="{card_id}">'
            f'<div class="card-header">'
            f"<h2>{safe_name}</h2>"
            f'<span class="site-count">{total} open site(s)</span>'
            f"</div>"
            f'<div class="table-container">'
            f"<table>"
            f"<thead><tr><th>Date</th><th>Available</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            f"</table>"
            f"</div>"
            f"{book_link}"
            f"</div>"
        )

    # Provide ISO format to be parsed by JS for local timezone conversion
    if scan_timestamp.tzinfo is None:
        scan_timestamp = scan_timestamp.replace(tzinfo=timezone.utc).astimezone()
    timestamp_iso = scan_timestamp.isoformat()
    timestamp_str = html.escape(scan_timestamp.strftime("%b %-d, %Y at %-I:%M %p"))

    if not cards_html:
        body_content = (
            '<div class="no-results">\U0001f6d1 No availability found in the current scan.</div>'
        )
        nav_content = ""
        calendar_content = ""
    else:
        calendar_content = build_calendar_html(all_availabilities)
        nav_content = f'<nav class="quick-nav"><h3>Jump To</h3><ul>{"".join(nav_links)}</ul></nav>'
        body_content = "\n".join(cards_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campsite Availability</title>
<style>
:root {{
  --bg-color: #f8fafc;
  --text-color: #0f172a;
  --text-muted: #64748b;
  --card-bg: #ffffff;
  --border-color: #e2e8f0;
  --primary: #10b981;
  --primary-hover: #059669;
  --accent: #f59e0b;
}}
*,*::before,*::after{{box-sizing:border-box}}
html {{ scroll-behavior: smooth; }}
body{{
  margin:0;padding:32px 16px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg-color);color:var(--text-color);
  line-height:1.6;
}}
.container{{max-width:800px;margin:0 auto}}
header{{margin-bottom:32px;text-align:center;}}
h1{{font-size:2rem;margin:0 0 8px;font-weight:700;color:#1e293b;letter-spacing:-0.025em;}}
.timestamp{{color:var(--text-muted);font-size:0.95rem;margin:0}}

.quick-nav {{
  background:var(--card-bg);
  border:1px solid var(--border-color);
  border-radius:12px;
  padding:20px;
  margin-bottom:32px;
  box-shadow:0 1px 3px rgba(0,0,0,0.05);
}}
.quick-nav h3 {{ margin: 0 0 12px 0; font-size: 1.1rem; color: #334155; }}
.quick-nav ul {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin: 0; padding: 0; list-style: none;
}}
.quick-nav li {{ display: flex; align-items: baseline; gap: 8px; font-size: 0.95rem; }}
.quick-nav a {{ color: var(--primary); text-decoration: none; font-weight: 500; transition: color 0.15s; }}
.quick-nav a:hover {{ color: var(--primary-hover); text-decoration: underline; }}
.nav-count {{ background: #f1f5f9; color: var(--text-muted); padding: 2px 8px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }}

.calendar-container {{
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  background: var(--card-bg);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 32px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}}
.calendar-controls {{
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 12px;
  width: 100%;
}}
.calendar-controls select {{
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid var(--border-color);
  font-size: 1rem;
  background: #fff;
  cursor: pointer;
  outline: none;
}}
.calendar-controls select:focus {{
  border-color: var(--primary);
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
}}
.calendar-controls button {{
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid var(--primary);
  background: #fff;
  color: var(--primary);
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}}
.calendar-controls button:hover {{
  background: var(--primary);
  color: #fff;
}}
.calendar-controls button.nav-btn {{
  border-color: var(--border-color);
  color: var(--text-muted);
  padding: 6px 14px;
  font-size: 1.2rem;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}}
.calendar-controls button.nav-btn:hover:not(:disabled) {{
  background: #f1f5f9;
  border-color: #cbd5e1;
  color: var(--text-color);
}}
.calendar-controls button.nav-btn:disabled {{
  opacity: 0.4;
  cursor: not-allowed;
  background: #f8fafc;
}}
.calendar-month {{
  width: 100%;
  max-width: 400px;
}}
.calendar-table {{
  width: 100%;
  border-collapse: separate;
  border-spacing: 6px;
}}
.calendar-table th, .calendar-table td {{
  text-align: center;
  padding: 6px;
  border-bottom: none;
}}
.calendar-table th {{
  color: var(--text-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
}}
.calendar-empty {{
  color: transparent;
}}
.calendar-day {{
  color: #94a3b8;
  font-size: 0.9rem;
}}
.calendar-available {{
  background-color: var(--primary);
  color: #fff !important;
  font-weight: 600;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.9rem;
  transition: transform 0.1s, box-shadow 0.1s;
}}
.calendar-available:hover {{
  transform: scale(1.1);
}}
.calendar-available.selected-date {{
  box-shadow: 0 0 0 2px var(--card-bg), 0 0 0 4px var(--primary);
}}

.card{{
  background:var(--card-bg);
  border:1px solid var(--border-color);
  border-radius:12px;
  padding:24px;margin-bottom:24px;
  box-shadow:0 4px 6px -1px rgba(0,0,0,0.05),0 2px 4px -1px rgba(0,0,0,0.03);
  transition: transform 0.2s, box-shadow 0.2s;
  scroll-margin-top: 24px;
}}
.card:hover {{ box-shadow:0 10px 15px -3px rgba(0,0,0,0.05),0 4px 6px -2px rgba(0,0,0,0.025); transform:translateY(-1px); }}
.card-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:12px;border-bottom:1px solid #f1f5f9;padding-bottom:16px;}}
.card-header h2{{font-size:1.25rem;margin:0;color:#0f172a;line-height:1.3;}}
.site-count{{background:#ecfdf5;color:#047857;padding:4px 12px;border-radius:9999px;font-size:0.875rem;font-weight:600;white-space:nowrap;}}
.site-count-none{{background:#f1f5f9;color:var(--text-muted);padding:4px 12px;border-radius:9999px;font-size:0.875rem;font-weight:600;white-space:nowrap;}}
.card-unavailable{{opacity:0.75;}}
.card-unavailable .card-header{{border-bottom-color:var(--border-color);}}
.nav-none{{background:#f1f5f9;color:var(--text-muted);}}

.table-container {{ overflow-x: auto; margin-bottom: 20px; }}
table{{width:100%;border-collapse:separate;border-spacing:0;font-size:0.95rem}}
th{{text-align:left;padding:12px 16px;border-bottom:2px solid #e2e8f0;color:var(--text-muted);font-weight:600;text-transform:uppercase;font-size:0.75rem;letter-spacing:0.05em;}}
td{{padding:12px 16px;border-bottom:1px solid #f1f5f9;color:#334155;}}
tr:last-child td {{border-bottom:none;}}

.available-badge {{ font-weight: 600; color: var(--text-color); }}

.book-action {{ display: flex; justify-content: flex-end; margin-top: 8px; }}
.book-link{{
  display:inline-block;
  background:var(--primary);color:#fff;
  font-weight:600;font-size:0.95rem;text-decoration:none;
  padding:10px 20px;border-radius:8px;
  transition:background 0.2s, transform 0.1s, box-shadow 0.2s;
  box-shadow: 0 2px 4px rgba(16, 185, 129, 0.2);
}}
.book-link:hover{{background:var(--primary-hover);transform:translateY(-1px);box-shadow: 0 4px 6px rgba(16, 185, 129, 0.3);}}
.book-link:active{{transform:translateY(0);box-shadow: 0 1px 2px rgba(16, 185, 129, 0.2);}}
.no-results{{
  text-align:center;color:var(--text-muted);padding:64px 32px;
  font-size:1.2rem;background:var(--card-bg);border-radius:12px;
  border:1px dashed var(--border-color);
}}
@media(max-width:600px){{
  body{{padding:16px 12px}}
  .card{{padding:16px}}
  .card-header{{flex-direction:column;align-items:flex-start;}}
  th,td{{padding:10px 8px;font-size:0.9rem}}
  .quick-nav {{ padding: 16px; }}
  h1 {{ font-size: 1.75rem; }}
}}
@media(prefers-color-scheme: dark){{
  :root {{
    --bg-color: #0f172a;
    --text-color: #e2e8f0;
    --text-muted: #94a3b8;
    --card-bg: #1e293b;
    --border-color: #334155;
    --primary: #10b981;
    --primary-hover: #059669;
  }}
  h1 {{ color: #f8fafc; }}
  .quick-nav h3 {{ color: #e2e8f0; }}
  .nav-count {{ background: #334155; color: #cbd5e1; }}
  .calendar-controls select {{ background: #0f172a; color: #e2e8f0; }}
  .calendar-controls button {{ background: #1e293b; color: #10b981; }}
  .calendar-controls button:hover {{ background: #10b981; color: #fff; }}
  .calendar-controls button.nav-btn {{ background: #0f172a; color: #94a3b8; border-color: #334155; }}
  .calendar-controls button.nav-btn:hover:not(:disabled) {{ background: #1e293b; color: #e2e8f0; border-color: #475569; }}
  .calendar-controls button.nav-btn:disabled {{ opacity: 0.4; background: transparent; }}
  .calendar-day {{ color: #64748b; }}
  .card-header {{ border-bottom-color: #334155; }}
  .card-header h2 {{ color: #f8fafc; }}
  .site-count {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
  .site-count-none {{ background: #334155; color: #94a3b8; }}
  .nav-none {{ background: #334155; color: #94a3b8; }}
  th {{ border-bottom-color: #334155; }}
  td {{ border-bottom-color: #334155; color: #e2e8f0; }}
}}
</style>
</head>
<body>
<div class="container">
<header>
<h1>&#x1F3D5; Campsite Availability</h1>
<p class="timestamp">Last updated: <span id="last-updated" data-timestamp="{timestamp_iso}">{timestamp_str}</span></p>
</header>
{calendar_content}
{nav_content}
{body_content}
</div>
<script>
document.addEventListener("DOMContentLoaded", () => {{
  const lastUpdated = document.getElementById("last-updated");
  if (lastUpdated) {{
    const ts = new Date(lastUpdated.getAttribute("data-timestamp"));
    lastUpdated.textContent = ts.toLocaleDateString(undefined, {{ month: 'short', day: 'numeric', year: 'numeric' }}) + ' at ' + ts.toLocaleTimeString(undefined, {{ hour: 'numeric', minute: '2-digit', hour12: true }});
  }}

  const monthSelector = document.getElementById("month-selector");
  const prevMonthBtn = document.getElementById("prev-month-btn");
  const nextMonthBtn = document.getElementById("next-month-btn");
  const clearBtn = document.getElementById("clear-date-filter");
  const allMonths = document.querySelectorAll(".calendar-month");
  const availCells = document.querySelectorAll(".calendar-available");
  const allCards = document.querySelectorAll(".card");
  
  function updateNavButtons() {{
    if (!monthSelector) return;
    const idx = monthSelector.selectedIndex;
    if (prevMonthBtn) prevMonthBtn.disabled = idx <= 0;
    if (nextMonthBtn) nextMonthBtn.disabled = idx >= monthSelector.options.length - 1;
  }}
  
  if (monthSelector) {{
    monthSelector.addEventListener("change", (e) => {{
      allMonths.forEach(m => m.style.display = "none");
      const selectedMonth = document.getElementById(e.target.value);
      if (selectedMonth) {{
        selectedMonth.style.display = "block";
      }}
      updateNavButtons();
    }});
    updateNavButtons();
  }}
  
  if (prevMonthBtn) {{
    prevMonthBtn.addEventListener("click", () => {{
      if (monthSelector.selectedIndex > 0) {{
        monthSelector.selectedIndex--;
        monthSelector.dispatchEvent(new Event("change"));
      }}
    }});
  }}
  
  if (nextMonthBtn) {{
    nextMonthBtn.addEventListener("click", () => {{
      if (monthSelector.selectedIndex < monthSelector.options.length - 1) {{
        monthSelector.selectedIndex++;
        monthSelector.dispatchEvent(new Event("change"));
      }}
    }});
  }}
  
  function filterByDate(dateStr) {{
    allCards.forEach(card => {{
      // Unavailable cards: show only when no date filter is active
      if (card.classList.contains("card-unavailable")) {{
        card.style.display = dateStr ? "none" : "";
        const navItem = document.querySelector(`.quick-nav li[data-ref="${{card.id}}"]`);
        if (navItem) navItem.style.display = dateStr ? "none" : "";
        return;
      }}

      const rows = card.querySelectorAll("tbody tr");
      let cardHasMatch = false;
      let visibleCount = 0;
      rows.forEach(row => {{
        if (!dateStr || row.getAttribute("data-date") === dateStr) {{
          row.style.display = "";
          cardHasMatch = true;
          visibleCount += parseInt(row.getAttribute("data-count") || "0", 10);
        }} else {{
          row.style.display = "none";
        }}
      }});
      const navItem = document.querySelector(`.quick-nav li[data-ref="${{card.id}}"]`);
      const siteCountSpan = card.querySelector(".site-count");
      const navCountSpan = navItem ? navItem.querySelector(".nav-count") : null;
      if (cardHasMatch) {{
        card.style.display = "";
        if (navItem) navItem.style.display = "";
        if (siteCountSpan) siteCountSpan.textContent = `${{visibleCount}} open site(s)`;
        if (navCountSpan) navCountSpan.textContent = visibleCount;
      }} else {{
        card.style.display = "none";
        if (navItem) navItem.style.display = "none";
      }}
    }});
    
    availCells.forEach(cell => {{
      if (dateStr && cell.getAttribute("data-date") === dateStr) {{
        cell.classList.add("selected-date");
      }} else {{
        cell.classList.remove("selected-date");
      }}
    }});
    
    if (dateStr) {{
      if(clearBtn) clearBtn.style.display = "inline-block";
    }} else {{
      if(clearBtn) clearBtn.style.display = "none";
    }}
  }}
  
  availCells.forEach(cell => {{
    cell.addEventListener("click", () => {{
      filterByDate(cell.getAttribute("data-date"));
    }});
  }});
  
  if (clearBtn) {{
    clearBtn.addEventListener("click", () => {{
      filterByDate(null);
    }});
  }}
}});
</script>
</body>
</html>"""


def write_dashboard(html_content: str, output_path: str) -> None:
    """Write HTML content to the specified file path."""
    with open(output_path, "w") as f:
        f.write(html_content)


def generate_dashboard(
    entries_with_results: list[tuple[dict, list[AvailableCampsite]] | ProcessedAvailability],
    day_filter: set[int] | None,
    output_path: str,
) -> str:
    """Build HTML, write to disk, and immediately free the string."""
    content = build_dashboard_html(entries_with_results, day_filter)
    write_dashboard(content, output_path)
    del content
    return output_path
