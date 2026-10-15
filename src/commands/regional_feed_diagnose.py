"""
One-shot, read-only comparison between a transit_system's own direct
511.org fetch and its demuxed slice of the combined agency=RG fetch - the
live verification IRL-15 needs before the agency=RG demux (see
src/services/regional_feed.py) can be trusted for real traffic. Makes four
real GTFS-RT requests (direct + RG, for both TripUpdates and ServiceAlerts)
and prints both comparison reports. Touches no database, no
RealtimeFeedCache, no scheduler state.

Run a handful of times by hand, spread out over different moments, rather
than in a loop - SF-MTA is currently the only member of the `511.org`
quota group, and its own existing QuotaGroupScheduler already consumes
close to the entire 55/hour internal budget (itself already below
511.org's real 60/hour cap) on its own. Looping this script would add a
second, uncoordinated source of real requests against the same external
limit.

Usage:
    API_KEY_511_ORG=... python -m src.commands.regional_feed_diagnose \\
        --transit-system SF-MTA --agency-code SF
"""

import argparse
import json
import logging
from typing import Dict, Tuple

import requests

from generated import gtfs_realtime_pb2
from src.services.regional_feed import (
    compare_alert_feeds,
    compare_feeds,
    split_feed_by_agency,
)
from src.settings import get_settings

logger = logging.getLogger(__name__)

TRIP_UPDATES_URL = "https://api.511.org/Transit/TripUpdates"
SERVICE_ALERTS_URL = "https://api.511.org/Transit/ServiceAlerts"


def _fetch(
    url: str, agency_code: str, api_key: str
) -> Tuple[gtfs_realtime_pb2.FeedMessage, Dict[str, str]]:
    response = requests.get(
        url,
        params={"api_key": api_key, "agency": agency_code},
        timeout=30,
    )
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed, dict(response.headers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transit-system",
        required=True,
        help="This app's TransitSystem.name for the shadow target, e.g. SF-MTA.",
    )
    parser.add_argument(
        "--agency-code",
        required=True,
        help="511.org agency code for the same system, e.g. SF.",
    )
    args = parser.parse_args()

    api_key = get_settings().api_key_511_org
    if not api_key:
        raise SystemExit(
            "API_KEY_511_ORG is not set - export it before running this script."
        )

    print("=== TripUpdates ===")
    direct_feed, direct_headers = _fetch(TRIP_UPDATES_URL, args.agency_code, api_key)
    rg_feed, rg_headers = _fetch(TRIP_UPDATES_URL, "RG", api_key)
    demuxed_by_system = split_feed_by_agency(
        rg_feed, {args.agency_code: args.transit_system}
    )
    demuxed_feed = demuxed_by_system.get(
        args.transit_system, gtfs_realtime_pb2.FeedMessage()
    )
    print(json.dumps(compare_feeds(direct_feed, demuxed_feed), indent=2))
    print()
    print(
        f"direct ({args.agency_code}) RateLimit-Remaining:",
        direct_headers.get("RateLimit-Remaining"),
    )
    print(
        "RG RateLimit-Remaining:               ", rg_headers.get("RateLimit-Remaining")
    )

    print()
    print("=== ServiceAlerts ===")
    direct_alert_feed, direct_alert_headers = _fetch(
        SERVICE_ALERTS_URL, args.agency_code, api_key
    )
    rg_alert_feed, rg_alert_headers = _fetch(SERVICE_ALERTS_URL, "RG", api_key)
    demuxed_alert_by_system = split_feed_by_agency(
        rg_alert_feed, {args.agency_code: args.transit_system}
    )
    demuxed_alert_feed = demuxed_alert_by_system.get(
        args.transit_system, gtfs_realtime_pb2.FeedMessage()
    )
    print(
        json.dumps(compare_alert_feeds(direct_alert_feed, demuxed_alert_feed), indent=2)
    )
    print()
    print(
        f"direct ({args.agency_code}) RateLimit-Remaining:",
        direct_alert_headers.get("RateLimit-Remaining"),
    )
    print(
        "RG RateLimit-Remaining:               ",
        rg_alert_headers.get("RateLimit-Remaining"),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    main()
