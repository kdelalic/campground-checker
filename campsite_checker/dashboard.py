import html
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Set

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

        rows_html = []
        for d in sorted(by_date):
            count = len(by_date[d])
            date_str = html.escape(d.strftime("%a, %b %-d"))
            rows_html.append(
                f"<tr>"
                f"<td>{date_str}</td>"
                f"<td>{count} site(s)</td>"
                f"</tr>"
            )

        book_link = ""
        if url:
            safe_url = html.escape(url)
            book_link = f'<a class="book-link" href="{safe_url}">Book now &rarr;</a>'

        cards_html.append(
            f'<div class="card">'
            f"<div class=\"card-header\">"
            f"<h2>{safe_name}</h2>"
            f"<span class=\"site-count\">{total} open site(s)</span>"
            f"</div>"
            f"<table>"
            f"<thead><tr><th>Date</th><th>Available</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            f"</table>"
            f"{book_link}"
            f"</div>"
        )

    timestamp_str = html.escape(scan_timestamp.strftime("%b %-d, %Y at %-I:%M %p"))

    if not cards_html:
        body_content = '<div class="no-results">No availability found in the current scan.</div>'
    else:
        body_content = "\n".join(cards_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campsite Availability</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{
  margin:0;padding:24px 16px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:#f5f5f4;color:#1c1917;
  line-height:1.5;
}}
.container{{max-width:720px;margin:0 auto}}
header{{margin-bottom:24px}}
h1{{font-size:1.5rem;margin:0 0 4px}}
.timestamp{{color:#78716c;font-size:0.875rem;margin:0}}
.card{{
  background:#fff;border:1px solid #e7e5e4;border-radius:8px;
  padding:16px;margin-bottom:16px;
}}
.card-header{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px;flex-wrap:wrap;gap:4px}}
.card-header h2{{font-size:1.1rem;margin:0}}
.site-count{{color:#78716c;font-size:0.875rem;white-space:nowrap}}
table{{width:100%;border-collapse:collapse;font-size:0.9rem}}
th{{text-align:left;padding:6px 8px;border-bottom:2px solid #e7e5e4;color:#78716c;font-weight:600}}
td{{padding:6px 8px;border-bottom:1px solid #f5f5f4}}
.book-link{{
  display:inline-block;margin-top:10px;
  color:#16a34a;font-weight:600;font-size:0.9rem;text-decoration:none;
}}
.book-link:hover{{text-decoration:underline}}
.no-results{{
  text-align:center;color:#78716c;padding:48px 16px;
  font-size:1.1rem;
}}
@media(max-width:480px){{
  body{{padding:16px 8px}}
  .card-header{{flex-direction:column}}
  th,td{{padding:4px 6px;font-size:0.85rem}}
}}
</style>
</head>
<body>
<div class="container">
<header>
<h1>&#x1F3D5; Campsite Availability</h1>
<p class="timestamp">Last updated: {timestamp_str}</p>
</header>
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
