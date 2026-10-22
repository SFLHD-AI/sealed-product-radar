from pathlib import Path

import pytest

from edge.config import Settings
from edge.service import EdgeService

DEMO = Path(__file__).parent / "fixtures" / "demo"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        tpx_api_key="tp_test_key",
        cache_dir=tmp_path / ".cache",
        mappings_path=tmp_path / "mappings.yaml",
        fixtures_dir=tmp_path / "fixtures",
        shipping_box_usd=15,
        shipping_case_usd=45,
        target_margin="0.08",
    )


@pytest.fixture
def service(settings):
    svc = EdgeService(settings, demo_fixtures=DEMO)
    yield svc
    svc.close()
