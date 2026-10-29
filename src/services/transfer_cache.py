"""
Global (not per-system) cache backing inter-system transfer resolution -
Phase 3's "which other transit systems share a physical station complex with
this stop_id" (see models.StopArea's docstring, and .claude/plans/
development.md's Phase 3 section). Deliberately separate from ScheduleCache
(src/services/schedule_cache.py): that cache is scoped per transit_system,
but answering "which OTHER systems" needs cross-system data that doesn't fit
its get(transit_system) shape - see area_ids_by_stop/members_by_area/
systems_by_stop below, each covering every active system at once, plus
regional_id_by_native_stop (IRL-25's lat/lng crosswalk - see
models.RegionalStopMatch) for the systems whose own stop_ids don't appear
in the regional feed verbatim.

Refreshed on the same 6-hour TTL as ScheduleCache, which is already far more
often than the underlying stop_areas.txt data (MTC-curated, refreshed daily -
see src/tasks.py's fetch_regional_stop_areas) could plausibly change.
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import select

from src.database import engine
from src.models import RegionalStopMatch, Stop, StopArea, TransitSystem

TTL = timedelta(hours=6)


class TransferCache:
    _area_ids_by_stop: Dict[str, List[str]] = {}
    _members_by_area: Dict[str, List[str]] = {}
    _systems_by_stop: Dict[str, Set[str]] = {}
    # (system name, native stop_id) -> regional stop_id - IRL-25's crosswalk,
    # only populated for the systems (today: just BART) whose own stop_ids
    # don't appear verbatim in the regional feed. See
    # models.RegionalStopMatch's docstring.
    _regional_id_by_native_stop: Dict[Tuple[str, str], str] = {}
    _loaded_at: Optional[datetime] = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get(
        cls,
    ) -> Tuple[
        Dict[str, List[str]],
        Dict[str, List[str]],
        Dict[str, Set[str]],
        Dict[Tuple[str, str], str],
    ]:
        if not cls._is_fresh():
            async with cls._lock:
                if not cls._is_fresh():
                    await asyncio.to_thread(cls._load)
        return (
            cls._area_ids_by_stop,
            cls._members_by_area,
            cls._systems_by_stop,
            cls._regional_id_by_native_stop,
        )

    @classmethod
    def _is_fresh(cls) -> bool:
        return cls._loaded_at is not None and datetime.utcnow() - cls._loaded_at < TTL

    @classmethod
    def _load(cls) -> None:
        with engine.begin() as connection:
            stop_area_rows = connection.execute(
                select(StopArea.area_id, StopArea.stop_id)
            ).all()
            # Every active system's own stop_id -> system name, the same
            # cross-system index this session's manual IRL-21 verification
            # built by hand (downloading each system's own stops.txt) - now
            # a single local join, since every system's Stop rows already
            # live in this same database.
            stop_rows = connection.execute(
                select(Stop.stop_id, TransitSystem.name)
                .select_from(Stop)
                .join(TransitSystem, TransitSystem.id == Stop.transit_system_id)
                .where(TransitSystem.active.is_(True))
            ).all()
            # IRL-25's crosswalk - empty for every system where stop_rows
            # above already matches the regional feed's own ids directly.
            match_rows = connection.execute(
                select(
                    TransitSystem.name,
                    RegionalStopMatch.stop_id,
                    RegionalStopMatch.regional_stop_id,
                )
                .select_from(RegionalStopMatch)
                .join(
                    TransitSystem,
                    TransitSystem.id == RegionalStopMatch.transit_system_id,
                )
                .where(TransitSystem.active.is_(True))
            ).all()

        area_ids_by_stop: Dict[str, List[str]] = defaultdict(list)
        members_by_area: Dict[str, List[str]] = defaultdict(list)
        for row in stop_area_rows:
            area_ids_by_stop[row.stop_id].append(row.area_id)
            members_by_area[row.area_id].append(row.stop_id)

        systems_by_stop: Dict[str, Set[str]] = defaultdict(set)
        for row in stop_rows:
            systems_by_stop[row.stop_id].add(row.name)

        regional_id_by_native_stop: Dict[Tuple[str, str], str] = {}
        for match_row in match_rows:
            regional_id_by_native_stop[(match_row.name, match_row.stop_id)] = (
                match_row.regional_stop_id
            )
            # The crosswalk's whole point: this system is only ever
            # discoverable as a transfer target under its *regional* id
            # (members_by_area only ever lists regional ids), so
            # systems_by_stop needs an entry there too, not just under its
            # native stop_id (which never appears in members_by_area for
            # these systems at all).
            systems_by_stop[match_row.regional_stop_id].add(match_row.name)

        cls._area_ids_by_stop = dict(area_ids_by_stop)
        cls._members_by_area = dict(members_by_area)
        cls._systems_by_stop = dict(systems_by_stop)
        cls._regional_id_by_native_stop = regional_id_by_native_stop
        cls._loaded_at = datetime.utcnow()


def resolve_inter_system_transfers(
    current_system: str,
    stop_id: str,
    area_ids_by_stop: Dict[str, List[str]],
    members_by_area: Dict[str, List[str]],
    systems_by_stop: Dict[str, Set[str]],
    regional_id_by_native_stop: Dict[Tuple[str, str], str],
) -> List[str]:
    """Pure in-memory lookup over TransferCache.get()'s dicts (no DB
    access) - every other transit system sharing a station complex with
    stop_id, sorted and deduped, excluding current_system itself. Confirmed
    live this session (IRL-21): e.g. "mtc:powell" groups BART's Powell St
    platform stops with SF-MTA's nearby Muni stops, so calling this for
    BART's own Powell St stop_id returns ["SF-MTA"].

    stop_id is always a system's own native id (what every other caller in
    this codebase works with) - regional_id_by_native_stop (IRL-25)
    translates it into the regional feed's id space first when a system's
    own ids don't appear there verbatim, since area_ids_by_stop/
    members_by_area are keyed by whatever the regional feed itself uses."""
    effective_stop_id = regional_id_by_native_stop.get(
        (current_system, stop_id), stop_id
    )
    other_systems: Set[str] = set()
    for area_id in area_ids_by_stop.get(effective_stop_id, []):
        for member_stop_id in members_by_area.get(area_id, []):
            if member_stop_id == effective_stop_id:
                continue
            other_systems.update(systems_by_stop.get(member_stop_id, set()))
    other_systems.discard(current_system)
    return sorted(other_systems)
