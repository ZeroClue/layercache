"""Unit tests for MetricsAggregator."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from layercache.metrics.aggregator import MetricsAggregator
from layercache.metrics.storage import MetricsDB


@pytest.fixture
async def aggregator(tmp_path: Path):
    """Create a MetricsAggregator with a temporary database."""
    db_path = str(tmp_path / "test_metrics.db")

    # Initialize DB first to create tables
    metrics_db = MetricsDB(db_path=db_path)
    await metrics_db.initialize()

    agg = MetricsAggregator(db_path=db_path)
    await agg.connect()
    yield agg
    await agg.close()
    await metrics_db.close()


@pytest.fixture
async def aggregator_with_data(tmp_path: Path):
    """Create a MetricsAggregator with test data."""
    db_path = str(tmp_path / "test_metrics.db")

    # Initialize DB first to create tables
    metrics_db = MetricsDB(db_path=db_path)
    await metrics_db.initialize()

    agg = MetricsAggregator(db_path=db_path)
    await agg.connect()

    # Insert test data - use SQLite-compatible datetime format
    now = datetime.now(UTC)
    # Use format that SQLite datetime() understands
    current_hour = now.strftime("%Y-%m-%d %H:00:00")
    current_hour_iso = now.strftime("%Y-%m-%dT%H:00:00")

    await metrics_db.insert_request(
        created_at=current_hour_iso,
        model="gpt-4o",
        session_id="session_1",
        semantic_cache_hit=True,
        duration_ms=150.0,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=800,
        cache_creation_tokens=200,
    )

    await metrics_db.insert_request(
        created_at=current_hour_iso,
        model="gpt-4o",
        session_id="session_2",
        semantic_cache_hit=False,
        duration_ms=200.0,
        input_tokens=1200,
        output_tokens=600,
        cache_read_tokens=0,
        cache_creation_tokens=1200,
    )

    await metrics_db.insert_request(
        created_at=current_hour_iso,
        model="claude-3-5-sonnet",
        session_id="session_1",
        semantic_cache_hit=True,
        duration_ms=180.0,
        input_tokens=900,
        output_tokens=450,
        cache_read_tokens=700,
        cache_creation_tokens=200,
    )

    yield agg
    await agg.close()
    await metrics_db.close()


@pytest.mark.asyncio
async def test_compute_hourly_rollup_empty(aggregator):
    """Test hourly rollup computation with empty database."""
    now = datetime.now(UTC)
    hour = now.strftime("%Y-%m-%dT%H:00:00")

    rollup = await aggregator.compute_hourly_rollup(hour)
    assert rollup is None


@pytest.mark.asyncio
async def test_compute_hourly_rollup_with_data(aggregator_with_data):
    """Test hourly rollup computation with test data."""
    now = datetime.now(UTC)
    hour = now.strftime("%Y-%m-%dT%H:00:00")

    rollup = await aggregator_with_data.compute_hourly_rollup(hour)

    assert rollup is not None
    assert rollup.hour == hour
    assert rollup.total_requests == 3
    assert rollup.cache_hits == 2
    assert rollup.cache_misses == 1
    assert rollup.total_input_tokens == 3100
    assert rollup.total_output_tokens == 1550
    assert rollup.cache_read_tokens == 1500
    assert rollup.cache_creation_tokens == 1600
    assert 150.0 <= rollup.avg_latency_ms <= 200.0


@pytest.mark.asyncio
async def test_compute_daily_rollup_unique_sessions(aggregator_with_data):
    """Test daily rollup correctly counts unique sessions."""
    now = datetime.now(UTC)
    date = now.strftime("%Y-%m-%d")

    rollup = await aggregator_with_data.compute_daily_rollup(date)

    assert rollup is not None
    assert rollup.date == date
    assert rollup.total_requests == 3
    assert rollup.unique_sessions == 2  # session_1 and session_2


@pytest.mark.asyncio
async def test_get_cache_hit_rate_zero_division(aggregator):
    """Test cache hit rate handles zero requests gracefully."""
    hit_rate = await aggregator.get_cache_hit_rate(hours=24)
    assert hit_rate == 0.0


@pytest.mark.asyncio
async def test_get_cache_hit_rate_calculation(aggregator_with_data):
    """Test cache hit rate calculation."""
    # First save hourly rollup
    now = datetime.now(UTC)
    hour = now.strftime("%Y-%m-%dT%H:00:00")

    rollup = await aggregator_with_data.compute_hourly_rollup(hour)
    if rollup:
        await aggregator_with_data.save_hourly_rollup(rollup)

    hit_rate = await aggregator_with_data.get_cache_hit_rate(hours=24)
    # 2 hits out of 3 requests = 66.67%
    assert 65.0 < hit_rate < 68.0


@pytest.mark.asyncio
async def test_get_token_savings_calculation(aggregator_with_data):
    """Test token savings calculation."""
    # First save hourly rollup
    now = datetime.now(UTC)
    hour = now.strftime("%Y-%m-%dT%H:00:00")

    rollup = await aggregator_with_data.compute_hourly_rollup(hour)
    if rollup:
        await aggregator_with_data.save_hourly_rollup(rollup)

    savings = await aggregator_with_data.get_token_savings(hours=24)

    assert savings["input_tokens_saved"] == 1500
    assert savings["output_tokens"] == 1550
    assert savings["total_tokens"] == 4650
    assert savings["savings_percentage"] > 0


@pytest.mark.asyncio
async def test_rollup_save_idempotent(aggregator_with_data):
    """Test that saving rollups is idempotent (INSERT OR REPLACE)."""
    now = datetime.now(UTC)
    hour = now.strftime("%Y-%m-%dT%H:00:00")

    rollup = await aggregator_with_data.compute_hourly_rollup(hour)
    assert rollup is not None

    # Save twice
    await aggregator_with_data.save_hourly_rollup(rollup)
    await aggregator_with_data.save_hourly_rollup(rollup)

    # Should still have only one row
    recent = await aggregator_with_data.get_recent_hourly(limit=1)
    assert len(recent) == 1
    assert recent[0].hour == hour


@pytest.mark.asyncio
async def test_get_recent_hourly_ordering(aggregator_with_data):
    """Test that recent hourly rollups are returned in descending order."""
    now = datetime.now(UTC)
    hour = now.strftime("%Y-%m-%dT%H:00:00")

    rollup = await aggregator_with_data.compute_hourly_rollup(hour)
    if rollup:
        await aggregator_with_data.save_hourly_rollup(rollup)

    recent = await aggregator_with_data.get_recent_hourly(limit=24)

    # Verify descending order
    for i in range(len(recent) - 1):
        assert recent[i].hour >= recent[i + 1].hour


@pytest.mark.asyncio
async def test_get_recent_daily(aggregator_with_data):
    """Test getting recent daily rollups."""
    now = datetime.now(UTC)
    date = now.strftime("%Y-%m-%d")

    rollup = await aggregator_with_data.compute_daily_rollup(date)
    if rollup:
        await aggregator_with_data.save_daily_rollup(rollup)

    recent = await aggregator_with_data.get_recent_daily(limit=30)

    assert len(recent) >= 1
    assert recent[0].date == date


@pytest.mark.asyncio
async def test_connect_error_handling(tmp_path: Path):
    """Test connection error handling with invalid path."""
    invalid_path = str(tmp_path / "nonexistent" / "test.db")
    agg = MetricsAggregator(db_path=invalid_path)

    # Should raise an exception due to non-existent directory
    with pytest.raises(Exception):
        await agg.connect()
