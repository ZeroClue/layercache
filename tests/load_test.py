"""LayerCache Load Testing Script.

Load testing for LayerCache v1.5.0 with Redis/SQLite backend.
Uses aiohttp for concurrent requests.

Test scenarios:
- Health endpoint: Basic availability
- Cache metrics: Metrics endpoint performance
- Chat completions: Main proxy endpoint (requires API key)

Metrics collected:
- Latency: p50, p95, p99
- Throughput: requests per second
- Error rate: percentage of failed requests
"""

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import aiohttp


@dataclass
class RequestResult:
    """Result of a single request."""

    latency_ms: float
    success: bool
    error: str | None = None
    status_code: int = 0


@dataclass
class TestResults:
    """Aggregated results for a test scenario."""

    scenario: str
    concurrent_users: int
    duration_seconds: int
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status_codes: dict[int, int] = field(default_factory=dict)

    @property
    def error_rate(self) -> float:
        """Calculate error rate as percentage."""
        if self.total_requests == 0:
            return 0.0
        return (self.failed_requests / self.total_requests) * 100

    @property
    def throughput(self) -> float:
        """Calculate requests per second."""
        if self.duration_seconds == 0:
            return 0.0
        return self.total_requests / self.duration_seconds

    def percentile(self, p: float) -> float:
        """Calculate latency percentile."""
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        index = int(len(sorted_latencies) * p / 100)
        return sorted_latencies[min(index, len(sorted_latencies) - 1)]

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def avg_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return statistics.mean(self.latencies)

    @property
    def min_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return min(self.latencies)

    @property
    def max_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return max(self.latencies)


class LoadTester:
    """Load testing engine for LayerCache."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
    ):
        self.base_url = base_url
        self.api_key = api_key

    def _get_headers(self, include_auth: bool = True) -> dict[str, str]:
        """Get request headers."""
        headers = {"Content-Type": "application/json"}
        if include_auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _make_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        include_auth: bool = True,
    ) -> RequestResult:
        """Make a single request and measure latency."""
        url = f"{self.base_url}{endpoint}"
        start_time = time.perf_counter()
        try:
            kwargs = {
                "headers": self._get_headers(include_auth),
                "timeout": aiohttp.ClientTimeout(total=30),  # TimeoutError returns status_code 0
            }
            if payload:
                kwargs["json"] = payload

            async with session.request(method, url, **kwargs) as response:
                latency_ms = (time.perf_counter() - start_time) * 1000
                await response.read()

                success = 200 <= response.status < 300
                return RequestResult(
                    latency_ms=latency_ms,
                    success=success,
                    status_code=response.status,
                    error=None if success else f"HTTP {response.status}",
                )
        except TimeoutError:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return RequestResult(
                latency_ms=latency_ms,
                success=False,
                status_code=0,
                error="Request timeout",
            )
        except aiohttp.ClientError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return RequestResult(
                latency_ms=latency_ms,
                success=False,
                status_code=0,
                error=str(e),
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return RequestResult(
                latency_ms=latency_ms,
                success=False,
                status_code=0,
                error=str(e),
            )

    async def _worker(
        self,
        session: aiohttp.ClientSession,
        worker_id: int,
        scenario: str,
        results: TestResults,
        stop_event: asyncio.Event,
        request_func: Callable,
    ):
        """Worker coroutine that makes requests until stopped."""
        request_count = 0
        while not stop_event.is_set():
            result = await request_func(session)

            results.total_requests += 1
            results.latencies.append(result.latency_ms)

            status = result.status_code
            results.status_codes[status] = results.status_codes.get(status, 0) + 1

            if result.success:
                results.successful_requests += 1
            else:
                results.failed_requests += 1
                if result.error:
                    results.errors.append(f"[Worker {worker_id}] {result.error}")

            request_count += 1
            await asyncio.sleep(0)

    async def run_scenario(
        self,
        scenario_name: str,
        concurrent_users: int,
        duration_seconds: int,
        request_func: Callable,
    ) -> TestResults:
        """Run a load test scenario."""
        results = TestResults(
            scenario=scenario_name,
            concurrent_users=concurrent_users,
            duration_seconds=duration_seconds,
        )

        stop_event = asyncio.Event()
        connector = aiohttp.TCPConnector(limit=concurrent_users * 2, ttl_dns_cache=300)

        async with aiohttp.ClientSession(connector=connector) as session:

            async def run_with_timeout():
                workers = [
                    asyncio.create_task(
                        self._worker(
                            session,
                            worker_id,
                            scenario_name,
                            results,
                            stop_event,
                            request_func,
                        )
                    )
                    for worker_id in range(concurrent_users)
                ]

                try:
                    await asyncio.sleep(duration_seconds)
                finally:
                    stop_event.set()
                    for worker in workers:
                        worker.cancel()
                        try:
                            await worker
                        except asyncio.CancelledError:
                            pass

            try:
                await asyncio.wait_for(run_with_timeout(), timeout=duration_seconds + 10)
            except TimeoutError:
                stop_event.set()

        return results


def print_results_table(results: list[TestResults]) -> None:
    """Print results in a formatted table."""
    print("\n" + "=" * 110)
    print("LOAD TEST RESULTS")
    print("=" * 110)

    header = (
        f"{'Scenario':<25} {'Users':<6} {'Duration':<8} {'Requests':<9} "
        f"{'Success':<8} {'Error%':<7} {'p50':<7} {'p95':<7} {'p99':<7} "
        f"{'Avg':<7} {'Min':<7} {'Max':<7} {'Throughput':<10}"
    )
    print(header)
    print("-" * 110)

    for r in results:
        row = (
            f"{r.scenario:<25} {r.concurrent_users:<6} {r.duration_seconds:<8} "
            f"{r.total_requests:<9} {r.successful_requests:<8} {r.error_rate:<7.2f} "
            f"{r.p50:<7.2f} {r.p95:<7.2f} {r.p99:<7.2f} "
            f"{r.avg_latency:<7.2f} {r.min_latency:<7.2f} {r.max_latency:<7.2f} "
            f"{r.throughput:<10.2f}"
        )
        print(row)

    print("=" * 110)


def print_status_code_breakdown(results: list[TestResults]) -> None:
    """Print status code breakdown."""
    print("\nSTATUS CODE BREAKDOWN")
    print("-" * 50)
    for r in results:
        if r.status_codes:
            codes = ", ".join(f"{k}:{v}" for k, v in sorted(r.status_codes.items()))
            print(f"{r.scenario} ({r.concurrent_users} users): {codes}")
    print("-" * 50)


def print_ascii_chart(results: list[TestResults], metric: str = "p95") -> None:
    """Print ASCII bar chart for a specific metric."""
    print(f"\n{metric.upper()} LATENCY BY SCENARIO (ms)")
    print("-" * 70)

    max_value = max((getattr(r, metric) for r in results), default=1)
    if max_value == 0:
        max_value = 1

    bar_width = 40

    for r in results:
        value = getattr(r, metric)
        bar_length = int((value / max_value) * bar_width)
        bar = "█" * bar_length + "░" * (bar_width - bar_length)
        label = f"{r.scenario} ({r.concurrent_users}u)"
        print(f"{label:<25} |{bar}| {value:.2f}")

    print("-" * 70)


def print_throughput_chart(results: list[TestResults]) -> None:
    """Print ASCII bar chart for throughput."""
    print("\nTHROUGHPUT BY SCENARIO (req/s)")
    print("-" * 70)

    max_value = max((r.throughput for r in results), default=1)
    if max_value == 0:
        max_value = 1

    bar_width = 40

    for r in results:
        bar_length = int((r.throughput / max_value) * bar_width)
        bar = "█" * bar_length + "░" * (bar_width - bar_length)
        label = f"{r.scenario} ({r.concurrent_users}u)"
        print(f"{label:<25} |{bar}| {r.throughput:.2f}")

    print("-" * 70)


async def run_load_tests(args) -> list[TestResults]:
    """Run all load test scenarios."""
    tester = LoadTester(
        base_url=args.base_url,
        api_key=args.api_key,
    )

    all_results: list[TestResults] = []

    print("=" * 60)
    print("LayerCache v1.5.0 Load Testing")
    print("=" * 60)
    print(f"Base URL: {args.base_url}")
    print(f"Duration per scenario: {args.duration}s")
    print(f"Concurrent users: {args.users}")
    print("=" * 60)

    for num_users in args.users:
        print(f"\n>>> Testing with {num_users} concurrent users")

        if not args.skip_health:
            print("  - Running /health endpoint test...")
            result = await tester.run_scenario(
                scenario_name="health",
                concurrent_users=num_users,
                duration_seconds=args.duration,
                request_func=lambda s: tester._make_request(s, "GET", "/health"),
            )
            all_results.append(result)
            print(
                f"    Completed: {result.total_requests} requests, "
                f"p95={result.p95:.2f}ms, throughput={result.throughput:.2f} req/s"
            )

        if not args.skip_metrics:
            print("  - Running /v1/cache/metrics endpoint test...")
            result = await tester.run_scenario(
                scenario_name="cache_metrics",
                concurrent_users=num_users,
                duration_seconds=args.duration,
                request_func=lambda s: tester._make_request(s, "GET", "/v1/cache/metrics"),
            )
            all_results.append(result)
            print(
                f"    Completed: {result.total_requests} requests, "
                f"p95={result.p95:.2f}ms, throughput={result.throughput:.2f} req/s"
            )

        if not args.skip_prometheus:
            print("  - Running /metrics (Prometheus) endpoint test...")
            result = await tester.run_scenario(
                scenario_name="prometheus_metrics",
                concurrent_users=num_users,
                duration_seconds=args.duration,
                request_func=lambda s: tester._make_request(s, "GET", "/metrics"),
            )
            all_results.append(result)
            print(
                f"    Completed: {result.total_requests} requests, "
                f"p95={result.p95:.2f}ms, throughput={result.throughput:.2f} req/s"
            )

        if not args.skip_chat and args.api_key:
            print("  - Running /v1/chat/completions endpoint test...")

            def chat_request(session):
                payload = {
                    "model": args.model,
                    "messages": [{"role": "user", "content": "Test query"}],
                    "max_tokens": 10,
                }
                return tester._make_request(
                    session, "POST", "/v1/chat/completions", payload=payload, include_auth=True
                )

            result = await tester.run_scenario(
                scenario_name="chat_completions",
                concurrent_users=num_users,
                duration_seconds=args.duration,
                request_func=chat_request,
            )
            all_results.append(result)
            print(
                f"    Completed: {result.total_requests} requests, "
                f"p95={result.p95:.2f}ms, throughput={result.throughput:.2f} req/s"
            )

    return all_results


async def main():
    """Main entry point for load testing."""
    parser = argparse.ArgumentParser(description="LayerCache Load Testing")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="LayerCache base URL",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for authentication (required for /v1/chat/completions)",
    )
    parser.add_argument(
        "--model",
        default="opencode-go/qwen3.5-plus",
        help="Model to use for chat completions",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Test duration in seconds per scenario",
    )
    parser.add_argument(
        "--users",
        nargs="+",
        type=int,
        default=[10, 50, 100],
        help="Number of concurrent users",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file for JSON results",
    )
    parser.add_argument(
        "--skip-health",
        action="store_true",
        help="Skip health endpoint tests",
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Skip cache metrics endpoint tests",
    )
    parser.add_argument(
        "--skip-prometheus",
        action="store_true",
        help="Skip Prometheus metrics endpoint tests",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="Skip chat completions endpoint tests",
    )

    args = parser.parse_args()

    all_results = await run_load_tests(args)

    print_results_table(all_results)
    print_status_code_breakdown(all_results)
    print_ascii_chart(all_results, "p95")
    print_throughput_chart(all_results)

    if args.output:
        output_data = {
            "test_config": {
                "base_url": args.base_url,
                "duration_seconds": args.duration,
                "concurrent_users": args.users,
            },
            "results": [
                {
                    "scenario": r.scenario,
                    "concurrent_users": r.concurrent_users,
                    "duration_seconds": r.duration_seconds,
                    "total_requests": r.total_requests,
                    "successful_requests": r.successful_requests,
                    "failed_requests": r.failed_requests,
                    "error_rate": r.error_rate,
                    "latency_p50": r.p50,
                    "latency_p95": r.p95,
                    "latency_p99": r.p99,
                    "latency_avg": r.avg_latency,
                    "latency_min": r.min_latency,
                    "latency_max": r.max_latency,
                    "throughput": r.throughput,
                    "status_codes": r.status_codes,
                }
                for r in all_results
            ],
        }
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\nResults saved to: {args.output}")

    return all_results


if __name__ == "__main__":
    asyncio.run(main())
