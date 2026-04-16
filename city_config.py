"""
城市配置管理模組

從 cities.json 載入多城市設定。
空白的 shared_drive_id / group_id 會從 .env 回退。
"""
import json
import os
from pathlib import Path

_CITIES_FILE = Path(__file__).parent / 'cities.json'


def load_cities() -> list:
    with open(_CITIES_FILE, encoding='utf-8') as f:
        return json.load(f)['cities']


def get_enabled_cities() -> list:
    return [c for c in load_cities() if c.get('enabled', True)]


def get_city(city_id: str) -> dict:
    for c in load_cities():
        if c['id'] == city_id:
            return c
    raise ValueError(f"City '{city_id}' not found in cities.json")


def resolve_city(city: dict) -> dict:
    """Fill in blank shared_drive_id / group_id from .env defaults."""
    from config import SHARED_DRIVE_ID, GROUP_ID
    resolved = dict(city)
    if not resolved.get('shared_drive_id'):
        resolved['shared_drive_id'] = SHARED_DRIVE_ID
    if not resolved.get('group_id'):
        resolved['group_id'] = GROUP_ID
    return resolved


def get_cities_for_cli(city_arg: str = None) -> list:
    """Parse --city CLI arg. None or 'all' -> all enabled; specific id -> single city."""
    if city_arg is None or city_arg == 'all':
        cities = get_enabled_cities()
    else:
        cities = [get_city(city_arg)]
    return [resolve_city(c) for c in cities]
