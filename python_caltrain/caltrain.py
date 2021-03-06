#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import csv
from collections import namedtuple
from datetime import datetime, timedelta
import pkg_resources
import re
from zipfile import ZipFile
from enum import Enum, unique
from io import TextIOWrapper

Train = namedtuple('Train', ['name', 'kind', 'direction',
                             'stops', 'service_window'])
Station = namedtuple('Station', ['name', 'zone', 'latitude', 'longitude'])
Stop = namedtuple('Stop', ['arrival', 'arrival_day',
                           'departure', 'departure_day',
                           'stop_number'])
ServiceWindow = namedtuple('ServiceWindow', ['id', 'name', 'start', 'end', 'days', 'removed'])

_BASE_DATE = datetime(1970, 1, 1, 0, 0, 0, 0)


class Trip(namedtuple('Trip', ['departure', 'arrival', 'duration', 'train'])):

    def __str__(self):
        return "[%s %s] Departs: %s, Arrives: %s (%s)" % \
                (str(self.train.kind), self.train.name, str(self.departure),
                 str(self.arrival), str(self.duration))

    def __unicode__(self):
        return unicode(self.__str__())

    def __repr__(self):
        return "Trip(departure=%s, arrival=%s, duration=%s, " \
               "train=Train(name=%s))" % \
                (repr(self.departure), repr(self.arrival),
                 repr(self.duration), self.train.name)


def _sanitize_name(name):
    """
    Pre-sanitization to increase the likelihood of finding
    a matching station.

    :param name: the station name
    :type name: str or unicode

    :returns: sanitized station name
    """
    return ''.join(re.split('[^A-Za-z0-9]', name)).lower()\
             .replace('station', '').strip()


def _resolve_time(t):
    """
    Resolves the time string into datetime.time. This method
    is needed because Caltrain arrival/departure time hours
    can exceed 23 (e.g. 24, 25), to signify trains that arrive
    after 12 AM. The 'day' variable is incremented from 0 in
    these situations, and the time resolved back to a valid
    datetime.time (e.g. 24:30:00 becomes days=1, 00:30:00).

    :param t: the time to resolve
    :type t: str or unicode

    :returns: tuple of days and datetime.time
    """
    hour, minute, second = [int(x) for x in t.split(":")]
    day, hour = divmod(hour, 24)
    r = _BASE_DATE + timedelta(hours=hour,
                               minutes=minute,
                               seconds=second)
    return day, r.time()


def _resolve_duration(start, end):
    """
    Resolves the duration between two times. Departure/arrival
    times that exceed 24 hours or cross a day boundary are correctly
    resolved.

    :param start: the time to resolve
    :type start: Stop
    :param end: the time to resolve
    :type end: Stop

    :returns: tuple of days and datetime.time
    """
    start_time = _BASE_DATE + timedelta(hours=start.departure.hour,
                                        minutes=start.departure.minute,
                                        seconds=start.departure.second,
                                        days=start.departure_day)
    end_time = _BASE_DATE + timedelta(hours=end.arrival.hour,
                                      minutes=end.arrival.minute,
                                      seconds=end.arrival.second,
                                      days=end.departure_day)
    return end_time - start_time


_STATIONS_RE = re.compile(r'^(.+) Caltrain( Station)?$')

_RENAME_MAP = {
    'SO. SAN FRANCISCO': 'SOUTH SAN FRANCISCO',
    'MT VIEW': 'MOUNTAIN VIEW',
    'CALIFORNIA AVE': 'CALIFORNIA AVENUE'
}

_DEFAULT_GTFS_FILE = 'data/caltrain_gtfs_latest.zip'
_ALIAS_MAP_RAW = {
    'SAN FRANCISCO': ('SF', 'SAN FRAN'),
    'SOUTH SAN FRANCISCO': ('S SAN FRANCISCO', 'SOUTH SF',
                            'SOUTH SAN FRAN', 'S SAN FRAN',
                            'S SAN FRANCISCO', 'S SF', 'SO SF',
                            'SO SAN FRANCISCO', 'SO SAN FRAN'),
    '22ND STREET': ('TWENTY-SECOND STREET', 'TWENTY-SECOND ST',
                    '22ND ST', '22ND', 'TWENTY-SECOND', '22'),
    'MOUNTAIN VIEW': 'MT VIEW',
    'CALIFORNIA AVENUE': ('CAL AVE', 'CALIFORNIA', 'CALIFORNIA AVE',
                          'CAL', 'CAL AV', 'CALIFORNIA AV'),
    'REDWOOD CITY': 'REDWOOD',
    'SAN JOSE DIRIDON': ('DIRIDON', 'SAN JOSE', 'SJ DIRIDON', 'SJ'),
    'COLLEGE PARK': 'COLLEGE',
    'BLOSSOM HILL': 'BLOSSOM',
    'MORGAN HILL': 'MORGAN',
    'HAYWARD PARK': 'HAYWARD',
    'MENLO PARK': 'MENLO'
}

_ALIAS_MAP = {}

for k, v in _ALIAS_MAP_RAW.items():
    if not isinstance(v, list) and not isinstance(v, tuple):
        v = (v,)
    for x in v:
        _ALIAS_MAP[_sanitize_name(x)] = _sanitize_name(k)


@unique
class Direction(Enum):
    north = 0
    south = 1


@unique
class TransitType(Enum):
    # route_short_name from routes.txt
    baby_bullet = "Bullet"
    limited = "Limited"
    local = "Local"
    tamien_sanjose = "TaSJ-Shuttle"
    special = "Special"
    bus_bridge = "Bus Bridge"

    def __str__(self):
        return self.name


class UnexpectedGTFSLayoutError(Exception):
    pass


class UnknownStationError(Exception):
    pass


class Caltrain(object):

    def __init__(self, gtfs_path=None):

        self.version = None
        self.trains = {}
        self.stations = {}
        self._unambiguous_stations = {}
        self._service_windows = {}
        self._fares = {}

        self.load_from_gtfs(gtfs_path)

    def load_from_gtfs(self, gtfs_path=None):
        """
        Loads a GTFS zip file and builds the data model from it.
        If not specified, the internally stored GTFS zip file from
        Caltrain is used instead.

        :param gtfs_path: the path of the GTFS zip file to load
        :type gtfs_path: str or unicode
        """
        # Use the default path if not specified.
        if gtfs_path is None:
            gtfs_path = pkg_resources\
                .resource_stream(__name__, _DEFAULT_GTFS_FILE)

        z = ZipFile(gtfs_path)

        self.trains, self.stations = {}, {}
        self._service_windows, self._fares = {}, {}

        # -------------------
        # 1. Record fare data
        # -------------------

        fare_lookup = {}

        # Create a map if (start, dest) -> price
        with z.open('fare_attributes.txt', 'r') as csvfile:
            fare_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in fare_reader:
                fare_lookup[r['fare_id']] = \
                    tuple(int(x) for x in r['price'].split('.'))

        # Read in the fare IDs from station X to station Y.
        with z.open('fare_rules.txt', 'r') as csvfile:
            fare_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in fare_reader:
                k = (r['origin_id'], r['destination_id'])
                self._fares[k] = fare_lookup[r['fare_id']]

        # ------------------------
        # 2. Record calendar dates
        # ------------------------

        # Record the days when certain trains are active.
        with z.open('calendar.txt', 'r') as csvfile:
            calendar_reader = csv.DictReader(TextIOWrapper(csvfile))
            keys = ('monday', 'tuesday', 'wednesday', 'thursday',
                    'friday', 'saturday', 'sunday')
            for r in calendar_reader:
                self._service_windows[r['service_id']] = ServiceWindow(
                    id=r['service_id'],
                    name=r['service_name'],
                    start=datetime.strptime(r['start_date'], '%Y%m%d').date(),
                    end=datetime.strptime(r['end_date'], '%Y%m%d').date(),
                    days=set(i for i, k in enumerate(keys) if int(r[k]) == 1),
                    removed=False,
                )

        # Account for some exceptions to calendar.txt
        with z.open('calendar_dates.txt', 'r') as csvfile:
            calendar_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in calendar_reader:
                service_id = r['service_id']
                service_date = r['date']
                holiday_name = r['holiday_name']
                exception_type = r['exception_type']
                # exception type: 1 indicates service added, 2 indicates service
                # removed
                if service_id not in self._service_windows:
                    service_date = datetime.strptime(
                        service_date, '%Y%m%d').date()
                    self._service_windows[service_id] = ServiceWindow(
                        id=service_id,
                        name=holiday_name,
                        start=service_date,
                        end=service_date,
                        days=set([service_date.weekday()]),
                        removed=exception_type == "2",
                    )

        # ------------------
        # 3. Record stations
        # ------------------
        with z.open('stops.txt', 'r') as csvfile:
            trip_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in trip_reader:
                # Non-numeric stop IDs are useless information as
                # can be observed and should therefore be skipped.
                if not r['stop_id'].isdigit():
                    continue
                stop_name = _STATIONS_RE.match(r['stop_name'])\
                    .group(1).strip().upper()
                self.stations[r['stop_id']] = {
                    'name': _RENAME_MAP.get(stop_name, stop_name).title(),
                    'zone': r['zone_id'],
                    'latitude': float(r['stop_lat']),
                    'longitude': float(r['stop_lon'])
                }

        # ---------------------------
        # 4. Record train definitions
        # ---------------------------
        routes = {}
        with z.open('routes.txt', 'r') as csvfile:
            route_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in route_reader:
              routes[r['route_id']] = r

        with z.open('trips.txt', 'r') as csvfile:
            train_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in train_reader:
                train_dir = int(r['direction_id'])
                route = routes[r['route_id']]
                name = r['trip_short_name'] or r['trip_id']
                assert name
                transit_type = TransitType(route['route_short_name'])
                self.trains[r['trip_id']] = Train(
                    name=name,
                    kind=transit_type,
                    direction=Direction(train_dir),
                    stops={},
                    service_window=self._service_windows[r['service_id']]
                )

        self.stations = dict(
            (k, Station(v['name'], v['zone'], v['latitude'], v['longitude']))
            for k, v in self.stations.items())

        # -----------------------
        # 5. Record trip stations
        # -----------------------
        with z.open('stop_times.txt', 'r') as csvfile:
            stop_times_reader = csv.DictReader(TextIOWrapper(csvfile))
            for r in stop_times_reader:
                stop_id = r['stop_id']
                train = self.trains[r['trip_id']]
                arrival_day, arrival = _resolve_time(r['arrival_time'])
                departure_day, departure = _resolve_time(r['departure_time'])
                train.stops[self.stations[stop_id].name] =\
                    Stop(arrival=arrival, arrival_day=arrival_day,
                         departure=departure, departure_day=departure_day,
                         stop_number=int(r['stop_sequence']))

        # For display
        self.stations = \
            dict(('_'.join(re.split('[^A-Za-z0-9]', v.name)).lower(), v)
                 for _, v in self.stations.items())

        # For station lookup by string
        self._unambiguous_stations = dict((k.replace('_', ''), v)
                                          for k, v in self.stations.items())

    def get_station(self, name):
        """
        Attempts to resolves a station name from a string into an
        actual station. An UnknownStationError is thrown if no
        Station can be derived

        :param name: the name to resolve
        :type name: str or unicode

        :returns: the resolved Station object
        """
        sanitized = _sanitize_name(name)
        sanitized = _ALIAS_MAP.get(sanitized, sanitized)
        station = self._unambiguous_stations.get(sanitized, None)
        if station:
            return station
        else:
            raise UnknownStationError(name)

    def get_trains(self, name, after=None):
        """
        Returns a list of possible trains with the given name and on the
        after date.

        :param name: the name to resolve
        :type name: str or unicode
        :param after: the time to find the train
                      (default datetime.now())
        :type after: datetime

        :returns: a list of possible trains
        """

        if after is None:
            after = datetime.now()

        possibilities = []

        for train in self.trains.values():

            if name != train.name:
                continue

            sw = train.service_window
            in_time_window = (sw.start <= after.date() <= sw.end
                              and after.weekday() in sw.days)
            if sw.removed:
                # removed and in time window
                if in_time_window:
                    continue
            else:
                # added but not in time window
                if not in_time_window:
                    continue

            possibilities.append(train)

        return possibilities

    def fare_between(self, a, b):
        """
        Returns the fare to travel between stations a and b. Caltrain fare
        is always dependent on the distance and not the train type.

        :param a: the starting station
        :type a: str or unicode or Station
        :param b: the destination station
        :type b: str or unicode or Station

        :returns: tuple of the dollar and cents cost
        """
        a = self.get_station(a) if not isinstance(a, Station) else a
        b = self.get_station(b) if not isinstance(b, Station) else b
        return self._fares[(a.zone, b.zone)]

    def next_trips(self, a, b, after=None):
        """
        Returns a list of possible trips to get from stations a to b
        following the after date. These are ordered from soonest to
        latest and terminate at the end of the Caltrain's 'service day'.

        :param a: the starting station
        :type a: str or unicode or Station
        :param b: the destination station
        :type b: str or unicode or Station
        :param after: the time to find the next trips after
                      (default datetime.now())
        :type after: datetime

        :returns: a list of possible trips
        """

        if after is None:
            after = datetime.now()

        a = self.get_station(a) if not isinstance(a, Station) else a
        b = self.get_station(b) if not isinstance(b, Station) else b

        possibilities = []

        for name, train in self.trains.items():

            sw = train.service_window
            in_time_window = (sw.start <= after.date() <= sw.end
                              and after.weekday() in sw.days)
            if sw.removed:
                # removed and in time window
                if in_time_window:
                    continue
            else:
                # added but not in time window
                if not in_time_window:
                    continue

            # Check to see if the train's stops contains our stations
            if a.name not in train.stops or b.name not in train.stops:
                continue

            stop_a = train.stops[a.name]
            stop_b = train.stops[b.name]

            # Check to make sure this train is headed in the right direction.
            if stop_a.stop_number > stop_b.stop_number:
                continue

            # Check to make sure this train has not left yet.
            if stop_a.departure < after.time():
                continue

            possibilities += [Trip(
                                departure=stop_a.departure,
                                arrival=stop_b.arrival,
                                duration=_resolve_duration(stop_a, stop_b),
                                train=train
                              )]

        possibilities.sort(key=lambda x: x.departure)
        return possibilities

    def next_trains(self, a, after=None, direction=None):
        """
        Returns a list of the next trains leaving from station a
        following the after date. These are ordered from soonest to
        latest and terminate at the end of the Caltrain's 'service day'.

        :param a: the starting station
        :type a: str or unicode or Station
        :param after: the time to find the next trips after
                      (default datetime.now())
        :type after: datetime
        :param direction: the direction to find the next trips after
        :type direction: enum

        :returns: a list of possible trips
        """

        if after is None:
            after = datetime.now()

        a = self.get_station(a) if not isinstance(a, Station) else a

        possibilities = []

        for name, train in self.trains.items():

            sw = train.service_window
            in_time_window = (sw.start <= after.date() <= sw.end
                              and after.weekday() in sw.days)
            if sw.removed:
                # removed and in time window
                if in_time_window:
                    continue
            else:
                # added but not in time window
                if not in_time_window:
                    continue

            # Check to see if the train's stops contains our stations
            if a.name not in train.stops:
                continue

            stop_a = train.stops[a.name]

            # Check to make sure this train is headed in the right direction.
            if direction is not None and direction != train.direction:
                continue

            # Check to make sure this train has not left yet.
            if stop_a.departure < after.time():
                continue

            possibilities += [Trip(
                                departure=stop_a.departure,
                                arrival=stop_a.departure,
                                duration=0,
                                train=train
                              )]

        possibilities.sort(key=lambda x: x.departure)
        return possibilities
