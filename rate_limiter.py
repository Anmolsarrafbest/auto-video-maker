"""Local rate-limit spacing, daily quota caps, and HTTP backoff for free-tier APIs."""

from datetime import datetime
import json
from pathlib import Path
import random
import sys
import time
from zoneinfo import ZoneInfo

from filelock import FileLock
import requests

STATE_DIR = Path(__file__).resolve().parent / "state"
QUOTA_FILE = STATE_DIR / "quota_usage.json"
QUOTA_LOCK = STATE_DIR / "quota_usage.lock"

PROVIDER_LIMITS = {
    "gemini-3.1-flash-lite": {"min_interval": 4.5, "daily_cap": 450, "warn_at": 400},
    "gemini-4-hydrogen": {"min_interval": 13.0, "daily_cap": 18, "warn_at": 15},
    "pixabay": {"min_interval": 0.7, "daily_cap": None, "warn_at": None},
    "coverr": {"min_interval": 0.5, "daily_cap": None, "warn_at": None, "hourly_cap": 50},
    "internet_archive": {"min_interval": 1.0, "daily_cap": None, "warn_at": None},
    "pexels": {"min_interval": 0.5, "daily_cap": None, "warn_at": None},
    "wikimedia": {"min_interval": 0.5, "daily_cap": None, "warn_at": None},
    "nasa": {"min_interval": 0.5, "daily_cap": None, "warn_at": None},
}


def _today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


def _load_state(today):
    if QUOTA_FILE.is_file():
        try:
            data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                counts = data.get("counts", {}) if data.get("date") == today else {}
                last_called = data.get("last_called", {})
                if isinstance(counts, dict) and isinstance(last_called, dict):
                    return {"date": today, "counts": counts, "last_called": last_called,
                            "coverr_hour": data.get("coverr_hour", [])}
        except (OSError, ValueError):
            pass
    return {"date": today, "counts": {}, "last_called": {}}


def _save_state(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = QUOTA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(QUOTA_FILE)


def quota_status():
    """Return a snapshot of today's tracked provider request counts."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with FileLock(QUOTA_LOCK, timeout=30):
        return _load_state(_today())


def reserve_slot(provider_id, *, fallback_provider=None):
    """Reserve a rate-limited request slot, falling back or raising at daily RPD caps."""
    if provider_id not in PROVIDER_LIMITS:
        raise ValueError(f"Unsupported rate-limited provider: {provider_id}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    wait_seconds = 0.0
    active = provider_id
    with FileLock(QUOTA_LOCK, timeout=30):
        state = _load_state(_today())
        limits = PROVIDER_LIMITS[active]
        count = int(state["counts"].get(active, 0))
        if limits["daily_cap"] is not None and count >= limits["daily_cap"]:
            if fallback_provider and fallback_provider in PROVIDER_LIMITS:
                print(
                    f"Rate guard: {active} reached its daily cap ({count}/{limits['daily_cap']}); "
                    f"falling back to {fallback_provider}.",
                    file=sys.stderr,
                )
                active = fallback_provider
                limits = PROVIDER_LIMITS[active]
                count = int(state["counts"].get(active, 0))
            else:
                raise ValueError(f"Daily quota cap reached for {active} ({count}/{limits['daily_cap']}).")
        if limits["daily_cap"] is not None and count >= limits["daily_cap"]:
            raise ValueError(f"Daily quota cap reached for {active} ({count}/{limits['daily_cap']}).")
        if limits["warn_at"] is not None and count + 1 >= limits["warn_at"]:
            print(
                f"Rate guard warning: {active} request {count + 1}/{limits['daily_cap']} for today.",
                file=sys.stderr,
            )
        now = time.time()
        last = float(state["last_called"].get(active, 0.0) or 0.0)
        if last > now + 5:
            last = now
        target = max(now, last + limits["min_interval"])
        if "hourly_cap" in limits:
            recent = [stamp for stamp in state.get("coverr_hour", []) if stamp > target - 3600]
            if len(recent) >= limits["hourly_cap"]:
                raise ValueError(f"Hourly quota cap reached for {active}.")
            state["coverr_hour"] = [*recent, target]
        wait_seconds = max(0.0, target - now)
        state["last_called"][active] = target
        state["counts"][active] = count + 1
        _save_state(state)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return active


def request_with_backoff(method, url, *, provider_id, fallback_provider=None, fallback_url=None,
                         max_retries=3, **kwargs):
    """Send an HTTP request under rate-limit spacing with exponential backoff on 429/503."""
    active = reserve_slot(provider_id, fallback_provider=fallback_provider)
    target_url = fallback_url if (active != provider_id and fallback_url) else url
    sender = {"GET": requests.get, "POST": requests.post}.get(method.upper())
    for attempt in range(max_retries + 1):
        response = sender(target_url, **kwargs) if sender else requests.request(method, target_url, **kwargs)
        if response.status_code not in (429, 503) or attempt == max_retries:
            return response, active
        retry_after = response.headers.get("Retry-After", "").strip()
        try:
            delay = min(60.0, max(1.0, float(retry_after))) if retry_after else None
        except ValueError:
            delay = None
        if delay is None:
            delay = min(60.0, (2 ** attempt) * 2.0 + random.uniform(0.2, 0.8))
        print(
            f"{active} returned HTTP {response.status_code}; retrying in {delay:.1f}s "
            f"({attempt + 1}/{max_retries}).",
            file=sys.stderr,
        )
        time.sleep(delay)
        active = reserve_slot(active, fallback_provider=fallback_provider if active != fallback_provider else None)
        if active != provider_id and fallback_url:
            target_url = fallback_url
    return response, active


def gemini_endpoint(model):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def call_gemini(api_key, payload, *, model="gemini-3.1-flash-lite", fallback_model=None, timeout=40):
    """Send one rate-limited Gemini generateContent call, returning (response, active_model)."""
    fallback_url = gemini_endpoint(fallback_model) if fallback_model else None
    return request_with_backoff(
        "POST",
        gemini_endpoint(model),
        provider_id=model,
        fallback_provider=fallback_model,
        fallback_url=fallback_url,
        headers={"x-goog-api-key": api_key},
        json=payload,
        timeout=timeout,
    )
