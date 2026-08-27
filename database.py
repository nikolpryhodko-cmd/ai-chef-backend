"""
Supabase client initialization.

We use the `supabase-py` SDK. The SDK's client itself is synchronous under
the hood (it wraps `httpx`), so to keep the whole application truly async
and avoid blocking the event loop, every call into it from our services is
wrapped with `anyio.to_thread.run_sync`. This gives us async route handlers
end-to-end without depending on an unofficial async fork of the SDK.

The service-role ("secret") key is used everywhere on the backend because
all writes (counters, profiles) must bypass Row Level Security — the backend
is the trusted server-side component, never the end-user client.
"""
from functools import lru_cache

from supabase import Client, create_client

from config import get_settings


@lru_cache
def get_supabase_client() -> Client:
    """
    Returns a singleton Supabase client authenticated with the secret
    (service_role) key. Cached so we only open one client per process.
    """
    settings = get_settings()
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY)
