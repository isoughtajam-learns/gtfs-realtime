import asyncio

import requests
from fastapi import FastAPI, HTTPException
from fastapi.sse import EventSourceResponse
from starlette import status
from fastapi.middleware.cors import CORSMiddleware

from generated import gtfs_realtime_pb2
from src.constants import GTFS_URLS
from src.positioning import get_location
from src.models import TripPosition

app = FastAPI()

# Define the origins that are allowed to make requests to your backend
origins = [
    "http://localhost:3000",  # Default Create React App port
    "http://localhost:5173",  # Default Vite port
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,            # Allows specific origins
    allow_credentials=True,           # Allows cookies and credentials
    allow_methods=["*"],              # Allows all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],              # Allows all headers
)

@app.get("/trip_updates/{transit_system}", response_class=EventSourceResponse)
async def transit_feed(transit_system: str):
    gtfs_url = GTFS_URLS.get(transit_system)
    if not gtfs_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    while True:
        feed = gtfs_realtime_pb2.FeedMessage()
        response = requests.get(gtfs_url)
        try:
            feed.ParseFromString(response.content)
        except Exception as ex:
            print(f"Parse error with Feed Message: {ex}")
            continue

        for entity in feed.entity:
            if entity.HasField("trip_update"):
                trip_id = entity.trip_update.trip.trip_id
                vehicle = str(entity.trip_update.vehicle.label)
                position = get_location(entity.trip_update.stop_time_update)
                if not position:
                    continue

                yield TripPosition(
                    trip_id=trip_id,
                    stop_id=position.stop_id,
                    last_arrival=position.last_arrival,
                    next_arrival=position.next_arrival,
                    vehicle=vehicle,
                    status=position.status,
                )
        await asyncio.sleep(30)
