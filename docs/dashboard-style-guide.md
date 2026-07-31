# Dashboard style guide

This guide defines the visual and content language for the static campsite
availability dashboard. The dashboard is a quiet personal utility, not a
commercial product. It should feel warm and outdoorsy without becoming
decorative, promotional, or themed.

The source files are
[`dashboard.html.j2`](../campsite_checker/templates/dashboard.html.j2),
[`dashboard.css`](../campsite_checker/templates/dashboard.css), and
[`dashboard.js`](../campsite_checker/templates/dashboard.js). Keep this guide
and those files aligned when the interface changes.

## Principles

1. **Put information first.** Availability, scan state, filters, and booking
   actions should be visible before branding or explanation.
2. **Make cozy choices, not cozy decorations.** Warm colors, paper-like
   surfaces, and a restrained serif create the tone. Wood grain, torn edges,
   illustrations, and ornamental shapes do not.
3. **Use literal language.** Labels should say exactly what a value or action
   represents.
4. **Keep state explicit.** Available, unavailable, partial, failed, and stale
   states must be distinguishable in text as well as color.
5. **Keep the page flat and calm.** Borders and spacing establish hierarchy.
   Shadows, special surfaces, and accent colors should be rare.
6. **Preserve the personal-app scale.** The dashboard should feel like a useful
   page made for its owner, not a marketing site or enterprise analytics
   product.

## Voice and copy

Use short, factual labels and status messages. Prefer nouns for data labels and
verbs for controls.

| Use | Avoid |
| --- | --- |
| `Campground checker` | A product name plus a second oversized page title |
| `Open campgrounds` | `Your campsite watchlist` |
| `Next opening` | `Your next adventure awaits` |
| `Failed scans` | `Scan health: All clear` |
| `Availability calendar` | A kicker, title, and explanatory paragraph saying the same thing |
| `Refresh now` | `Stay up to date` |
| `No campgrounds match these filters.` | Promotional or apologetic empty-state copy |

Copy rules:

- Use one page title.
- Do not add slogans, taglines, eyebrow copy, advertisements, or aspirational
  language.
- Do not explain an obvious control. Add one short instruction only when the
  interaction would otherwise be unclear.
- Do not repeat a heading in a kicker or supporting sentence.
- Use warning symbols only for an actual warning, partial result, stale
  snapshot, or failure.
- Avoid exclamation marks unless they are part of campground data.

## Color

The palette takes its cues from the 🏕️ emoji: twilight blue, pine green, tent
orange, warm yellow, and cream. CSS custom properties in `dashboard.css` are
the canonical values.

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| `--canvas` | `#f2e5cf` | `#1d2928` | Page background |
| `--paper` | `#fff8e9` | `#283532` | Primary surfaces |
| `--paper-muted` | `#f5ead6` | `#303e39` | Secondary surfaces |
| `--ink` | `#332d27` | `#f3e8d3` | Primary text |
| `--ink-soft` | `#6d6257` | `#c0b4a1` | Secondary text |
| `--line` | `#d3bfa2` | `#4d5c52` | Standard borders |
| `--line-strong` | `#bda486` | `#68766b` | Controls and emphasis |
| `--twilight` | `#2d4056` | `#283d50` | Masthead |
| `--twilight-deep` | `#223244` | `#172936` | Strong masthead edge |
| `--pine` | `#31563f` | `#91b796` | Availability |
| `--pine-deep` | `#203a2b` | `#b5d0b5` | Links and strong green |
| `--pine-soft` | `#e1e9dc` | `rgb(101 151 111 / 18%)` | Green tint |
| `--tent` | `#cf6735` | `#c86736` | Primary actions and selection |
| `--tent-deep` | `#a94e28` | `#e58955` | Orange hover and emphasis |
| `--tent-soft` | `#f5dfcc` | `rgb(207 103 53 / 17%)` | Orange tint |
| `--sun` | `#f0c66a` | `#e6bd62` | Counts and warnings |
| `--sun-soft` | `#f8ebc8` | `rgb(230 189 98 / 14%)` | Warning surface |
| `--bark` | `#765b43` | `#d0b497` | Utility labels |

Use color by meaning:

- Twilight is the masthead color, not a general-purpose card color.
- Pine means current availability or a supporting booking link.
- Tent orange marks the primary booking action, focus, or active selection.
- Warm yellow holds compact counts and warning emphasis.
- Paper and line colors do most of the grouping work.
- Do not add a new hard-coded color when an existing semantic token fits.
- Do not introduce purple gradients, electric blues, neon accents, or
  high-saturation status colors.

Dark mode should preserve these semantic roles rather than invert every light
value mechanically.

## Typography

Body text and controls use the system UI stack:

```css
-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif
```

Headings and prominent values use:

```css
"Iowan Old Style", Charter, "Palatino Linotype", Georgia, serif
```

The serif provides warmth and hierarchy. It is not display advertising.

- App title: `1.48rem`, weight `700`; `1.35rem` on small screens.
- Section title: `1.72rem`, weight `700`.
- Campground title: `1.34rem`, weight `700`.
- Summary values: `clamp(1.35rem, 2.2vw, 1.8rem)`.
- Most labels and utility text: `0.68rem` to `0.86rem`.
- Default line height: `1.5`.

Avoid oversized editorial headlines, handwriting fonts, wide uppercase
tracking, or serif body copy.

## Spacing, borders, and shape

- Maximum content width is `1040px`.
- Desktop page padding is `24px 18px 72px`.
- Small-screen page padding is `9px 8px 44px`.
- Use the existing radius tokens: `7px`, `11px`, and `15px`.
- Use `1px` borders for ordinary grouping. The calendar may use a `2px` outer
  border.
- Use the existing spacing rhythm before adding a new value: `4`, `6`, `8`,
  `11`, `14`, `18`, `22`, `26`, and `36px`.
- Reserve full pills for small numeric counts. Labels, buttons, filters, and
  cards should remain modestly rectangular.
- Do not place bordered cards inside bordered cards unless the inner boundary
  represents a real interactive control or state.
- Do not use hover lifts. A hover state may change color or underline text
  without moving the element.

The flat calendar shadow is the exception, not a default surface treatment.
The masthead and campground cards stay unshadowed.

## Components

### Masthead

The masthead is compact utility chrome, not a hero.

It contains:

- the 🏕️ mark and one `Campground checker` heading;
- latest-scan age, exact timestamp, refresh cadence, and refresh action;
- three literal summary values: open campgrounds, next opening, and failed
  scans;
- the criteria used for the current scan.

Do not add a second title, slogan, introductory paragraph, promotional
description, decorative icon treatment, or nested freshness card. The 🏕️ emoji
is the only brand mark and should not receive a glow or drop shadow.
Use that same mark for the self-contained favicon, with Apple Color Emoji first
in its font fallback so it matches the masthead on macOS.

### Summary values

Render summary values as one divided strip. Use equal cells, quiet borders, and
literal labels. Do not turn each value into a floating metric card.

The values update when filters are active, so the summary IDs documented below
must remain stable.

### Calendar

- Keep the calendar in a compact, centered surface instead of stretching its
  outer card across the full content column.
- Cluster each available date with its count. Avoid distributing the two values
  across the full cell width.
- Use close, even gutters between dates. Preserve at least `43px` for actionable
  date cells on small screens even as the surrounding padding tightens.
- Available dates use solid pine.
- The count uses a small warm-yellow marker.
- Searched dates with no result use muted paper.
- Unsearched dates stay plain.
- The selected date receives a tent-orange outline.
- The legend repeats these meanings in text.

Calendar color must never be the only state signal. Each actionable date needs
an accessible label, and its selected state must use `aria-pressed`.

### Filters and controls

Controls are bordered, rectangular, and at least `43px` high where space
allows. Use visible labels. Orange indicates focus or a primary action, not
general decoration.

The result count is a compact status label. It may use a modest radius but
should not become a prominent badge.

### Campground cards

Cards are plain paper containers with:

- one campground heading;
- factual badges for nights, availability, partial data, or failure;
- an optional status message;
- an availability table;
- an optional booking footer.

Cards must not use `::before`, `::after`, a colored edge stripe, a glow, a
shadow, or a hover lift. State is already communicated by the badge, status
message, border, and content.

Date rows use native `details` and `summary` disclosure. Keep them collapsed by
default so a long result list stays scannable.

### Campground map

The map is a geographic index of campground-level status, not a campsite map.

- Show one marker per configured campground. Do not add individual campsite
  markers, routes, polygons, terrain layers, or decorative overlays.
- Fit the view to markers matching the active date, name, and status filters.
  Cap a single marker at campground scale rather than building scale.
- Recalculate the map size and visible bounds when its responsive container
  changes size.
- When provider coordinates are identical, group those campgrounds under one
  marker and list each campground in its popup.
- Use pine for available, muted paper for no availability, and warm yellow for
  failed or stale. Repeat those meanings in text and in the legend.
- Popup campground names link to the corresponding result card.
- Keep the accessible marker-data list in the document. Result cards remain
  the authoritative non-map representation.
- Use the OpenFreeMap Liberty vector style with visible attribution, customized
  to the dashboard's warm land, blue water, pine park/forest, and restrained
  road palette. Keep water fills and useful place labels, but suppress the
  `waterway_tunnel`, `waterway_river`, `waterway_other`, `boundary_2`,
  `boundary_3`, and `boundary_disputed` style layers for a quieter geographic
  index. Rely on normal browser caching and never add prefetch or offline
  downloads.
- Require two-finger map gestures on touch screens and Command/Ctrl plus scroll
  on desktop so normal page scrolling is not trapped by the map.

### Actions and links

- The campground booking action is solid tent orange.
- Supporting site links use pale pine.
- Text links underline on hover.
- Button labels use direct verbs such as `Refresh`, `Book`, `Show`, and `Clear`.
- Do not add generic calls to action such as `Get started` or `Learn more`.

### Empty, failed, partial, and stale states

These states describe data quality, not emotion.

- Empty means a completed search found no availability.
- Failed means current availability could not be determined.
- Partial means some provider requests failed and the results may be
  incomplete.
- Stale means retained results came from an earlier successful scan.

Every state needs explicit text. Never represent a failed scan as an empty
result, and never rely on amber, orange, or an icon alone.

## Responsive behavior

The layout has two maintained breakpoints:

### At `820px`

- The calendar keeps its compact centered width, while its inner panel uses all
  of the available calendar surface.
- Summary cells retain their compact layout.
- The map keeps the active marker bounds after its width changes.

### At `620px`

- Masthead and freshness information stack.
- Summary values become two columns, with the third spanning both.
- Search criteria stack into labeled rows.
- Search and status controls become one column.
- Calendar spacing and date cells become more compact.
- The map uses a fixed compact height and its legend wraps below it.
- Quick navigation scrolls horizontally.
- Card headings, badges, and individual site rows stack.
- The main booking action becomes full width.

Do not solve mobile layout problems by hiding useful scan or availability
information. Reflow it.

## Accessibility

- Use one `h1`, followed by logical `h2` and `h3` headings.
- Keep semantic `header`, `main`, `section`, `nav`, `article`, table, caption,
  and disclosure elements.
- Preserve the shared `3px` tent-orange focus ring.
- Keep controls keyboard operable and visibly focused.
- Keep calendar `aria-label` and `aria-pressed` values accurate.
- Keep result counts, selected-date text, filtered summary text, and stale
  warnings in their current live regions or status roles.
- Hide decorative emoji, dots, and swatches from assistive technology.
- Communicate every state through text in addition to color.
- Keep external booking links descriptive and use `rel="noopener"`.
- Preserve `prefers-reduced-motion` and `prefers-color-scheme` support.
- Check that touch targets remain usable when controls stack on small screens.

## Implementation constraints

The dashboard is a single HTML artifact. Application CSS/JavaScript and the
vendored MapLibre runtime are inlined during Jinja rendering. OpenFreeMap map
styles, vector/raster tiles, glyphs, and sprites are the only permitted
external asset requests. Do not add:

- external fonts, icon libraries, or non-map image requests;
- a frontend build step or framework;
- JavaScript-only access to essential availability data;
- template syntax inside `dashboard.js`.

These hooks are part of the current JavaScript contract:

| Hook | Purpose |
| --- | --- |
| `.freshness-card` | Receives the `is-stale` state |
| `#last-updated`, `#relative-age` | Freshness calculation and display |
| `#snapshot-stats`, `#summary-*` | Dynamic summary values |
| `.calendar-available button` | Date filtering and selection |
| `data-date`, `data-label`, `aria-pressed` | Calendar filter state |
| `.card[data-state][data-name][data-openings]` | Result filtering and counts |
| `.quick-nav li[data-ref][data-state][data-name]` | Quick-navigation filtering |
| `#campground-search`, `#status-filter` | Result controls |
| `#month-selector`, `#date-filter` | Calendar controls |
| `#campground-map`, `#map-marker-data` | Map initialization and marker data |
| `data-card-id`, `data-latitude`, `data-longitude` | Marker-to-card mapping |
| `data-map-visible` | Result-filter and visible-marker synchronization |

Rename or remove a hook only when `dashboard.js` and its focused tests are
updated in the same change.

## Anti-patterns

Do not add:

- slogans, taglines, advertisements, or product-marketing copy;
- oversized hero headlines or billboard layouts;
- gradients, glass effects, backdrop blur, glows, or neon;
- abstract blobs, rings, floating shapes, or decorative pseudo-elements;
- colored card-edge stripes;
- shadows on every surface;
- hover lifts or springy card motion;
- pills for ordinary labels and controls;
- large metric cards with decorative icons;
- faux-rustic textures, handwriting, wood grain, or torn-paper effects;
- extra emoji beyond the single 🏕️ identity mark and real warning states;
- redundant kickers, headings, and descriptions;
- generic status copy such as `Everything you care about` or `All clear`.

If an element does not communicate data, state, navigation, or an action,
remove it.

## Pre-merge checklist

- [ ] The first viewport prioritizes scan and availability data.
- [ ] There is one page title and no promotional copy.
- [ ] Every border, background, icon, and badge has a functional purpose.
- [ ] Cards have no decorative pseudo-elements or edge stripes.
- [ ] New colors use the existing semantic tokens.
- [ ] Available, empty, partial, failed, and stale states remain distinct.
- [ ] Keyboard focus and screen-reader labels still work.
- [ ] Light mode, dark mode, `820px`, and `620px` layouts are considered.
- [ ] The page does not overflow horizontally.
- [ ] The dashboard remains one HTML artifact; only live map resources load externally.
- [ ] Focused dashboard tests cover new behavior or protected markup.
- [ ] Formatting, linting, and the full test suite pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```
