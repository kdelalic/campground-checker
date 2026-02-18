#!/usr/bin/env python3
"""
check_campsites.py - Check campsite availability using camply.

Usage:
    python check_campsites.py                              # uses campsites.yaml, next 3 months, Saturdays only
    python check_campsites.py -c my_sites.yaml             # custom config file
    python check_campsites.py --start 2026-05-01 --end 2026-07-01
    python check_campsites.py --nights 2                   # override minimum nights
    python check_campsites.py --day Friday Saturday        # check specific days
    python check_campsites.py --all-days                   # check all days of the week
    python check_campsites.py --forever                    # poll continuously (default: every 5 min)
    python check_campsites.py --forever --interval 10      # poll every 10 minutes
"""

from campsite_checker.runner import main

if __name__ == "__main__":
    main()
