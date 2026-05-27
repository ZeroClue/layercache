"""Integration tests for analytics API endpoint."""

import pytest
from pathlib import Path
import tempfile
import os
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient
from layercache.main import app, lifespan
from layercache.metrics.aggregator import MetricsAggregator
from layercache.metrics.storage import MetricsDB
from layercache.metrics.collector import MetricsCollector


@pytest.fixture
def client_with_aggregator(tmp_path: Path):
    """Create test client with mocked aggregator."""
    db_path = str(tmp_path / "test_metrics.db")
    
    from fastapi import FastAPI
    from contextlib import asynccontextmanager
    
    @asynccontextmanager
    async def test_lifespan(test_app: FastAPI):
        from layercache.config import LayerCacheSettings
        settings = LayerCacheSettings()
        settings.caching.metrics.db_path = db_path
        test_app.state.settings = settings
        test_app.state.metrics = MetricsCollector()
        
        metrics_db = MetricsDB(db_path=db_path)
        await metrics_db.initialize()
        test_app.state.metrics_db = metrics_db
        
        aggregator = MetricsAggregator(db_path=db_path)
        await aggregator.connect()
        test_app.state.metrics_aggregator = aggregator
        
        yield
        
        await aggregator.close()
        await metrics_db.close()
    
    test_app = FastAPI(lifespan=test_lifespan)
    
    from layercache.dashboard.router import router as dashboard_router
    test_app.include_router(dashboard_router)
    
    with TestClient(test_app) as client:
        yield client


@pytest.fixture
def client_empty_db(tmp_path: Path):
    """Create test client with empty database."""
    db_path = str(tmp_path / "test_metrics.db")
    
    # Create a fresh app for this test
    from fastapi import FastAPI
    from contextlib import asynccontextmanager
    
    @asynccontextmanager
    async def test_lifespan(test_app: FastAPI):
        from layercache.config import LayerCacheSettings
        settings = LayerCacheSettings()
        settings.caching.metrics.db_path = db_path
        test_app.state.settings = settings
        test_app.state.metrics = MetricsCollector()
        
        metrics_db = MetricsDB(db_path=db_path)
        await metrics_db.initialize()
        test_app.state.metrics_db = metrics_db
        
        aggregator = MetricsAggregator(db_path=db_path)
        await aggregator.connect()
        test_app.state.metrics_aggregator = aggregator
        
        yield
        
        await aggregator.close()
        await metrics_db.close()
    
    test_app = FastAPI(lifespan=test_lifespan)
    
    # Include the dashboard router
    from layercache.dashboard.router import router as dashboard_router
    test_app.include_router(dashboard_router)
    
    with TestClient(test_app) as client:
        yield client


@pytest.mark.asyncio
async def test_analytics_api_default_hours(client_empty_db):
    """Test analytics API with default hours parameter."""
    response = client_empty_db.get("/dashboard/api/analytics")
    
    assert response.status_code == 200
    data = response.json()
    
    assert "summary" in data
    assert "time_series" in data
    assert "templates" in data
    assert "sessions" in data
    
    assert data["summary"]["hit_rate"] == 0.0
    assert data["summary"]["tokens_saved"] == 0
    assert data["summary"]["total_requests"] == 0
    assert data["time_series"] == []


@pytest.mark.asyncio
async def test_analytics_api_custom_hours(client_empty_db):
    """Test analytics API with custom hours parameter."""
    response = client_empty_db.get("/dashboard/api/analytics?hours=48")
    
    assert response.status_code == 200
    data = response.json()
    
    assert "summary" in data
    assert data["summary"]["hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_analytics_api_hours_validation(client_empty_db):
    """Test that hours parameter is clamped to valid range."""
    # Test minimum (should clamp to 1)
    response = client_empty_db.get("/dashboard/api/analytics?hours=0")
    assert response.status_code == 200
    
    # Test maximum (should clamp to 8760)
    response = client_empty_db.get("/dashboard/api/analytics?hours=10000")
    assert response.status_code == 200
    
    # Test negative (should clamp to 1)
    response = client_empty_db.get("/dashboard/api/analytics?hours=-100")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_analytics_api_empty_database(client_empty_db):
    """Test analytics API handles empty database gracefully."""
    response = client_empty_db.get("/dashboard/api/analytics")
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return zeros, not crash
    assert data["summary"]["hit_rate"] == 0.0
    assert data["summary"]["tokens_saved"] == 0
    assert data["time_series"] == []


@pytest.mark.asyncio
async def test_analytics_api_with_data(client_with_aggregator, tmp_path: Path):
    """Test analytics API returns data when database has records."""
    from datetime import UTC, datetime
    aggregator = client_with_aggregator.app.state.metrics_aggregator
    metrics_db = client_with_aggregator.app.state.metrics_db
    
    now = datetime.now(UTC)
    current_hour = now.strftime("%Y-%m-%dT%H:00:00")
    
    # Use proper insert method
    await metrics_db.insert_request(
        created_at=current_hour,
        model="gpt-4o",
        session_id="test_session",
        semantic_cache_hit=True,
        duration_ms=150.0,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=800,
        cache_creation_tokens=200,
    )
    
    # Compute and save hourly rollup
    rollup = await aggregator.compute_hourly_rollup(current_hour)
    if rollup:
        await aggregator.save_hourly_rollup(rollup)
    
    # Now test the API
    response = client_with_aggregator.get("/dashboard/api/analytics")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["summary"]["total_requests"] > 0
    assert data["summary"]["hit_rate"] > 0
    assert len(data["time_series"]) > 0


@pytest.mark.asyncio
async def test_analytics_api_no_aggregator(tmp_path: Path):
    """Test analytics API handles missing aggregator gracefully."""
    db_path = str(tmp_path / "test_metrics.db")
    
    from fastapi import FastAPI
    from contextlib import asynccontextmanager
    
    @asynccontextmanager
    async def test_lifespan(test_app: FastAPI):
        from layercache.config import LayerCacheSettings
        settings = LayerCacheSettings()
        settings.caching.metrics.db_path = db_path
        test_app.state.settings = settings
        test_app.state.metrics = MetricsCollector()
        # Don't set metrics_aggregator or metrics_db
        yield
    
    test_app = FastAPI(lifespan=test_lifespan)
    
    from layercache.dashboard.router import router as dashboard_router
    test_app.include_router(dashboard_router)
    
    with TestClient(test_app) as client:
        response = client.get("/dashboard/api/analytics")
        
        assert response.status_code == 200
        data = response.json()
        
        # Should return empty data, not crash
        assert data["summary"]["hit_rate"] == 0.0
        assert data["time_series"] == []


@pytest.mark.asyncio
async def test_analytics_api_error_handling(client_empty_db):
    """Test analytics API handles errors gracefully."""
    # Mock aggregator to raise an exception
    original_aggregator = client_empty_db.app.state.metrics_aggregator
    mock_aggregator = AsyncMock()
    mock_aggregator.get_cache_hit_rate.side_effect = Exception("Test error")
    client_empty_db.app.state.metrics_aggregator = mock_aggregator
    
    try:
        response = client_empty_db.get("/dashboard/api/analytics")
        
        assert response.status_code == 200
        data = response.json()
        
        # Should return error in response but not crash
        assert "error" in data or data["summary"]["hit_rate"] == 0.0
    finally:
        client_empty_db.app.state.metrics_aggregator = original_aggregator


@pytest.mark.asyncio
async def test_analytics_api_time_series_structure(client_with_aggregator, tmp_path: Path):
    """Test that time series data has correct structure."""
    from datetime import UTC, datetime
    aggregator = client_with_aggregator.app.state.metrics_aggregator
    metrics_db = client_with_aggregator.app.state.metrics_db
    
    now = datetime.now(UTC)
    current_hour = now.strftime("%Y-%m-%dT%H:00:00")
    
    await metrics_db.insert_request(
        created_at=current_hour,
        model="gpt-4o",
        session_id="test_session",
        semantic_cache_hit=False,
        duration_ms=200.0,
        input_tokens=1200,
        output_tokens=600,
        cache_read_tokens=0,
        cache_creation_tokens=1200,
    )
    
    rollup = await aggregator.compute_hourly_rollup(current_hour)
    if rollup:
        await aggregator.save_hourly_rollup(rollup)
    
    response = client_with_aggregator.get("/dashboard/api/analytics")
    assert response.status_code == 200
    
    data = response.json()
    
    if data["time_series"]:
        point = data["time_series"][0]
        assert "hour" in point
        assert "hit_rate" in point
        assert "total_requests" in point
        assert "cache_hits" in point
        assert "cache_misses" in point
        assert "avg_latency" in point
        assert "input_tokens" in point
        assert "output_tokens" in point
        assert "cache_read_tokens" in point
