import html
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

from camply.containers import AvailableCampsite

from .results import filter_results, get_booking_url, get_facility_name


def get_dashboard_path(args, config: dict) -> Optional[str]:
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


def build_dashboard_html(
    entries_with_results: List[Tuple[dict, List[AvailableCampsite]]],
    day_filter: Optional[Set[int]],
    scan_timestamp: Optional[datetime] = None,
) -> str:
    """Generate a complete self-contained HTML string."""
    if scan_timestamp is None:
        scan_timestamp = datetime.now()

    cards_html = []
    nav_links = []
    for entry, results in entries_with_results:
        filtered = filter_results(results, day_filter)
        if not filtered:
            continue

        name = get_facility_name(filtered)
        url = get_booking_url(filtered)

        by_date: Dict[date, List[AvailableCampsite]] = defaultdict(list)
        for r in filtered:
            by_date[r.booking_date.date()].append(r)

        total = sum(len(v) for v in by_date.values())
        safe_name = html.escape(name)
        card_id = f"site-{len(cards_html)}"

        nav_links.append(f'<li><a href="#{card_id}">{safe_name}</a> <span class="nav-count">{total}</span></li>')

        rows_html = []
        for d in sorted(by_date):
            count = len(by_date[d])
            date_str = html.escape(d.strftime("%a, %b %-d"))
            rows_html.append(
                f"<tr>"
                f"<td>{date_str}</td>"
                f"<td><span class=\"available-badge\">{count} site(s)</span></td>"
                f"</tr>"
            )

        book_link = ""
        if url:
            safe_url = html.escape(url)
            book_link = f'<div class="book-action"><a class="book-link" href="{safe_url}" target="_blank">Book now &rarr;</a></div>'

        cards_html.append(
            f'<div class="card" id="{card_id}">'
            f"<div class=\"card-header\">"
            f"<h2>{safe_name}</h2>"
            f"<span class=\"site-count\">{total} open site(s)</span>"
            f"</div>"
            f"<div class=\"table-container\">"
            f"<table>"
            f"<thead><tr><th>Date</th><th>Available</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            f"</table>"
            f"</div>"
            f"{book_link}"
            f"</div>"
        )

    timestamp_str = html.escape(scan_timestamp.strftime("%b %-d, %Y at %-I:%M %p"))

    if not cards_html:
        body_content = '<div class="no-results">\U0001f6d1 No availability found in the current scan.</div>'
        nav_content = ''
    else:
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
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0; padding: 0; list-style: none;
}}
.quick-nav li {{ display: flex; align-items: baseline; gap: 8px; font-size: 0.95rem; }}
.quick-nav a {{ color: var(--primary); text-decoration: none; font-weight: 500; transition: color 0.15s; }}
.quick-nav a:hover {{ color: var(--primary-hover); text-decoration: underline; }}
.nav-count {{ background: #f1f5f9; color: var(--text-muted); padding: 2px 8px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }}

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

.table-container {{ overflow-x: auto; margin-bottom: 20px; }}
table{{width:100%;border-collapse:separate;border-spacing:0;font-size:0.95rem}}
th{{text-align:left;padding:12px 16px;border-bottom:2px solid #e2e8f0;color:var(--text-muted);font-weight:600;text-transform:uppercase;font-size:0.75rem;letter-spacing:0.05em;}}
td{{padding:12px 16px;border-bottom:1px solid #f1f5f9;color:#334155;}}
tr:last-child td {{border-bottom:none;}}

.available-badge {{ font-weight: 600; color: #1e293b; }}

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
</style>
</head>
<body>
<div class="container">
<header>
<h1>&#x1F3D5; Campsite Availability</h1>
<p class="timestamp">Last updated: {timestamp_str}</p>
</header>
{nav_content}
{body_content}
</div>
</body>
</html>"""


def write_dashboard(html_content: str, output_path: str) -> None:
    """Write HTML content to the specified file path."""
    with open(output_path, "w") as f:
        f.write(html_content)


def generate_dashboard(
    entries_with_results: List[Tuple[dict, List[AvailableCampsite]]],
    day_filter: Optional[Set[int]],
    output_path: str,
) -> str:
    """Build HTML, write to disk, return the path written."""
    content = build_dashboard_html(entries_with_results, day_filter)
    write_dashboard(content, output_path)
    return output_path
