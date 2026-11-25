from __future__ import annotations

import httpx
import pytest

from api_wrapper.api.app import create_app
from api_wrapper.config.loader import load_project_file
from api_wrapper.converter.build import convert, write_outputs
from api_wrapper.mock_upstream import create_mock_app
from api_wrapper.sample.make_workbook import write_sample


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("sample")
    write_sample(d)
    return d


@pytest.fixture(scope="session")
def converted(sample_dir):
    """Sample workbook converted WITH overrides -> (result, config dir, config path)."""
    res = convert(sample_dir / "sample_mapping.xlsx", "demo", sample_dir / "overrides.yml")
    paths = write_outputs(res, sample_dir / "configs")
    return res, sample_dir / "configs", paths["config"]


@pytest.fixture()
def demo_cfg(converted):
    return load_project_file(converted[2])


@pytest.fixture()
def client(converted, demo_cfg):
    """Wrapper app wired to the in-process mock upstream."""
    from fastapi.testclient import TestClient

    transport = httpx.ASGITransport(app=create_mock_app(demo_cfg, lines=3))
    app = create_app(str(converted[1]), environ={"UPSTREAM_DEMO_BASE_URL": "http://mock"}, transport=transport)
    with TestClient(app) as c:
        yield c
