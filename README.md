# campsite-checker

Check campsite availability across Recreation.gov, Yellowstone, California State Parks, and GoingToCamp using the [camply](https://github.com/juftin/camply) library.

## Setup

```bash
pip install -r requirements.txt
```

## Configuration

Copy the example config and fill in your campground IDs:

```bash
cp campsites.example.yaml campsites.yaml
```

Each entry in the `campsites` list needs at minimum a `campground_id` or `recreation_area`:

```yaml
campsites:
  - name: "Yosemite - Upper Pines"
    provider: RecreationDotGov   # default if omitted
    campground_id: 232447
    nights: 2                    # minimum consecutive nights (default: 1)
```

See [campsites.example.yaml](campsites.example.yaml) for all options and providers.

## Usage

```bash
# Check all campsites — next 3 months, Saturdays only (default)
python check_campsites.py

# Use a different config file
python check_campsites.py -c my_campsites.yaml

# Override date range
python check_campsites.py --start 2026-06-01 --end 2026-08-31

# Check specific days
python check_campsites.py --day Friday Saturday

# Check all days of the week
python check_campsites.py --all-days

# Require at least 2 consecutive nights (overrides config)
python check_campsites.py --nights 2

# Show camply's internal search logs
python check_campsites.py --verbose

# Poll continuously every 5 minutes (Ctrl+C to stop)
python check_campsites.py --forever

# Poll every 10 minutes
python check_campsites.py --forever --interval 10
```

## Search Performance Tuning

Compatible campgrounds are searched in bounded batches, and batches run
concurrently. The defaults are four workers, four campgrounds per batch, and no
extra submission delay:

```bash
# More concurrency for a host with sufficient memory and provider headroom
python check_campsites.py --workers 6

# Smaller batches, or disable batching with --batch-size 1
python check_campsites.py --batch-size 2

# Pace requests to each provider independently
python check_campsites.py --search-delay 1

# Disable periodic forced GC, or choose a different scan interval
python check_campsites.py --gc-interval 0
```

Each scan logs per-provider batch counts, median duration, slowest duration, and
total scan time. Use those measurements together with error rates and memory
usage when tuning the values for a deployment.

In continuous mode, stable Recreation.gov campsite metadata is cached for up to
24 hours. Dashboard files and R2 objects are only updated when semantic
availability changes; failed uploads are retried without rebuilding unchanged
HTML. Full garbage collection runs every 12 scans by default and logs its
duration, object count, and resident-memory measurement.

## Telegram Notifications

Get a message when availability is found.

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID: send your bot a message, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"id"` in `"chat"`.
3. Pass credentials via flags, environment variables, or `campsites.yaml` (pick one):

```bash
# Via CLI flags
python check_campsites.py --telegram-token "123:ABC..." --telegram-chat-id "456789"

# Via environment variables
export TELEGRAM_BOT_TOKEN="123:ABC..."
export TELEGRAM_CHAT_ID="456789"
python check_campsites.py
```

```yaml
# Via campsites.yaml (lowest priority — CLI flags and env vars override this)
telegram:
  bot_token: "123:ABC..."
  chat_id: "456789"

campsites:
  - ...
```

```bash
# Combine with --forever for continuous monitoring with alerts
python check_campsites.py --forever --interval 5
```

In `--forever` mode, Telegram messages are only sent for *newly appeared* availability
(i.e. sites that weren't found in the previous scan), so you won't get spammed.

## Finding Campground IDs

**Recreation.gov:** Visit the campground page — the URL contains `facilityId=XXXXX`. Or use camply:

```bash
camply campgrounds --provider RecreationDotGov --search "upper pines"
```

**Yellowstone:**

```bash
camply campgrounds --provider Yellowstone
```

**ReserveCalifornia:**

```bash
camply campgrounds --provider ReserveCalifornia --search "emerald bay"
```

**GoingToCamp (Canadian parks):**

```bash
camply recreation-areas --provider GoingToCamp --search "algonquin"
```

## Example Output

```text
Campsite Availability Check
Date range : 2026-02-16 to 2026-05-18
Days       : Saturday
Campsites  : 2

Searching: Yosemite - Upper Pines ... 5 result(s) found.

============================================================
  Yosemite - Upper Pines
============================================================

  Campground : Upper Pines
  Site       : 042 / A Loop
  Available  : 2 night(s)
    - Sat 2026-04-04
    - Sat 2026-05-09
  Book at    : https://www.recreation.gov/camping/campsites/XXXXX

  Total available nights: 2
```
