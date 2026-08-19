GTFS_URLS = {
    "BART": "http://api.bart.gov/gtfsrt/tripupdate.aspx",
    "Helsinki_Regional_Transport": "https://realtime.hsl.fi/realtime/trip-updates/v2/hsl",
    "MBTA": "https://cdn.mbta.com/realtime/TripUpdates.pb",
    "Estonia": "https://crowdsourcing.transitous.org/gtfsrt/ee-elron/trip-updates.pb",
    "Lisbon": "https://go.tmlmobilidade.pt/hub/api/v1/realtime/trip-updates/gtfs",
}

GTFS_METADATA = {
    "BART": "http://www.bart.gov/dev/schedules/google_transit.zip",
    "Helsinki_Regional_Transport": "http://dev.hsl.fi/gtfs/hsl.zip",
    "MBTA": "https://cdn.mbta.com/MBTA_GTFS.zip",
    "Estonia": "https://eu-gtfs.remix.com/elron.zip",
    "Lisbon": "https://go.tmlmobilidade.pt/hub/api/v1/plans/gtfs",
}

DEFAULT_SCHEDULE_URL_BY_SYSTEM = {
    "BART": "https://www.bart.gov/schedules",
    "Helsinki_Regional_Transport": "https://www.hsl.fi/en/timetables",
    "MBTA": "https://www.mbta.com/schedules/",
}
