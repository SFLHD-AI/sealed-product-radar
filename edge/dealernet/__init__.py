"""Dealernet price sources."""


def live_source(settings):
    """The automatic Dealernet backend picked by DEALERNET_ACCESS, or None for manual/csv."""
    if settings.dealernet_access == "api":
        from .api import ApiSource

        key = settings.dealernet_api_key.get_secret_value() if settings.dealernet_api_key else ""
        return ApiSource(key, settings.dealernet_api_url)
    if settings.dealernet_access == "scrape":
        from ..matching import Mappings
        from .scrape import ScrapeSource

        pw = settings.dealernet_password.get_secret_value() if settings.dealernet_password else ""
        return ScrapeSource(settings.dealernet_username or "", pw, settings.cache_dir, Mappings(settings.mappings_path))
    return None
