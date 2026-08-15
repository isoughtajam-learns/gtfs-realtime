import re

GTFS_URLS = {
    "BART": "http://api.bart.gov/gtfsrt/tripupdate.aspx",
    "Helsinki_Regional_Transport": "https://realtime.hsl.fi/realtime/trip-updates/v2/hsl",
    "Provence-Alpes": "https://www.data.gouv.fr/api/1/datasets/r/a19e1138-12a2-4760-b458-34953849d15a",
    "Eurostar": "https://www.data.gouv.fr/api/1/datasets/r/9c3a3d5c-a52c-451e-89c8-32822af20bee"
}

GTFS_METADATA = {
    "BART": "http://www.bart.gov/dev/schedules/google_transit.zip",
    "Helsinki_Regional_Transport": "http://dev.hsl.fi/gtfs/hsl.zip",
    "Provence-Alpes": "https://www.data.gouv.fr/api/1/datasets/r/0a554ecc-a9b7-490c-a4ad-bbd8a0dc0ded",
    "Eurostar": "https://www.data.gouv.fr/api/1/datasets/r/bfd97acd-63f3-4ea4-bfe8-70e4c7fd8d13"
}

DEFAULT_SCHEDULE_URL_BY_SYSTEM = {
    "BART": "https://www.bart.gov/schedules",
    "Helsinki_Regional_Transport": "https://www.hsl.fi/en/timetables",
}

DISALLOWED_CHAR = ['\ufeff']
DISALLOWED_CHARS_PATTERN = f"[{''.join(map(re.escape, DISALLOWED_CHAR))}]"
