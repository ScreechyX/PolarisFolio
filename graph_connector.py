"""
Microsoft Graph calendar connector.

Uses MSAL for OAuth2 device code flow (suitable for a CLI/local tool).
Pulls calendar events and normalises them into CalendarEvent objects.

Setup:
  1. Register an app in Azure AD (Entra ID):
     - Go to portal.azure.com > App registrations > New registration
     - Name it (e.g. "PolarisFolio Clone")
     - Supported account types: "Accounts in any org + personal Microsoft accounts"
     - No redirect URI needed for device code flow
  2. Under API permissions, add:
     - Calendars.Read (delegated)
  3. Copy the Application (client) ID into CLIENT_ID below.
     No client secret is required for public client / device code flow.
"""

import os
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests
import msal

from models import CalendarEvent, Attendee

# -- Configuration -----------------------------------------------------------
# Set these via environment variables or replace directly for local testing.
CLIENT_ID = os.environ.get("MS_CLIENT_ID", "YOUR_CLIENT_ID_HERE")

# Token cache file - persists login between runs
TOKEN_CACHE_FILE = os.path.expanduser("~/.polarisfolio_token_cache.json")

SCOPES = ["Calendars.Read", "User.Read"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Response status mapping from Graph API to friendly strings
RESPONSE_MAP = {
    "accepted": "accepted",
    "declined": "declined",
    "tentativelyAccepted": "tentative",
    "none": "unknown",
    "notResponded": "unknown",
}


# -- Token cache (persist login between runs) --------------------------------

def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE, "r") as f:
            cache.deserialize(f.read())
    return cache


def _save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())


def _get_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        CLIENT_ID,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )


# -- Authentication ----------------------------------------------------------

def authenticate() -> str:
    """
    Returns a valid access token. Uses cached token if available,
    otherwise initiates device code flow (user visits a URL and enters a code).
    """
    cache = _load_cache()
    app = _get_app(cache)

    accounts = app.get_accounts()
    result = None

    if accounts:
        # Try silent token refresh first
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        # Device code flow - user-friendly for CLI/local tools
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise ValueError(f"Failed to initiate device flow: {flow.get('error_description')}")

        print("\n--- Microsoft Sign-in ---")
        print(flow["message"])
        print()

        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        raise ValueError(f"Authentication failed: {result.get('error_description')}")

    return result["access_token"]


def sign_out():
    """Clears the cached token, forcing re-authentication next run."""
    if os.path.exists(TOKEN_CACHE_FILE):
        os.remove(TOKEN_CACHE_FILE)
        print("Signed out successfully.")


# -- Graph API helpers -------------------------------------------------------

def _graph_get(token: str, url: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


def _paginate(token: str, url: str, params: dict = None) -> list:
    """Handles Graph API pagination (@odata.nextLink)."""
    results = []
    next_url = url
    next_params = params

    while next_url:
        data = _graph_get(token, next_url, next_params)
        results.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
        next_params = None  # nextLink already contains query params

    return results


# -- Calendar data -----------------------------------------------------------

def get_calendars(token: str) -> list[dict]:
    """Returns all calendars the user has access to."""
    data = _graph_get(token, f"{GRAPH_BASE}/me/calendars")
    return data.get("value", [])


def get_events(
    token: str,
    start_date: datetime,
    end_date: datetime,
    calendar_id: str = None,
) -> list[CalendarEvent]:
    """
    Fetches events between start_date and end_date.
    If calendar_id is None, fetches from the default calendar.
    """
    # Format dates as ISO 8601 with UTC timezone
    start_str = start_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if calendar_id:
        url = f"{GRAPH_BASE}/me/calendars/{calendar_id}/calendarView"
    else:
        url = f"{GRAPH_BASE}/me/calendarView"

    params = {
        "startDateTime": start_str,
        "endDateTime": end_str,
        "$select": "id,subject,start,end,location,bodyPreview,attendees,isAllDay,calendar",
        "$orderby": "start/dateTime",
        "$top": 100,
    }

    raw_events = _paginate(token, url, params)
    return [_parse_graph_event(e) for e in raw_events]


def _parse_graph_event(raw: dict) -> CalendarEvent:
    """Converts a raw Graph API event dict into a CalendarEvent."""

    def parse_dt(dt_obj: dict) -> datetime:
        dt_str = dt_obj["dateTime"]
        tz_str = dt_obj.get("timeZone", "UTC")
        # Graph returns datetime without timezone info in the string itself
        # We parse and attach UTC, then convert if needed
        dt = datetime.fromisoformat(dt_str.rstrip("Z"))
        return dt.replace(tzinfo=timezone.utc)

    attendees = []
    for a in raw.get("attendees", []):
        email_obj = a.get("emailAddress", {})
        status = a.get("status", {}).get("response", "none")
        attendees.append(Attendee(
            name=email_obj.get("name", ""),
            email=email_obj.get("address", ""),
            response=RESPONSE_MAP.get(status, "unknown"),
        ))

    location = raw.get("location", {}).get("displayName", "") or None

    return CalendarEvent(
        id=raw.get("id", ""),
        title=raw.get("subject", "(No title)"),
        start=parse_dt(raw["start"]),
        end=parse_dt(raw["end"]),
        location=location,
        description=raw.get("bodyPreview", ""),
        attendees=attendees,
        is_all_day=raw.get("isAllDay", False),
        is_recurring=raw.get("type", "singleInstance") != "singleInstance",
        calendar_name="Microsoft 365",
        source="graph",
    )


# -- Quick test --------------------------------------------------------------

if __name__ == "__main__":
    print("Authenticating with Microsoft...")
    token = authenticate()

    print("\nFetching calendars...")
    calendars = get_calendars(token)
    for cal in calendars:
        print(f"  - {cal['name']} ({cal['id'][:8]}...)")

    print("\nFetching events for next 7 days...")
    now = datetime.now(timezone.utc)
    events = get_events(token, now, now + timedelta(days=7))

    print(f"\nFound {len(events)} events:")
    for e in events:
        print(f"  {e.start.strftime('%a %d %b')} {e.time_str} - {e.title}")
        if e.attendees:
            print(f"    Attendees: {', '.join(a.name for a in e.attendees)}")
