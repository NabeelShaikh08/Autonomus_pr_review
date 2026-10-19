"""Tests for the standalone worker process."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(delivery_id: str = "test-delivery") -> dict:
    return {
        "delivery_id": delivery_id,
        "payload": {
            "action": "opened",
            "pull_request": {"number": 1, "title": "Test", "body": "", "head": {"sha": "abc"}},
            "repository": {"name": "repo", "owner": {"login": "user"}},
            "installation": {"id": 42},
        },
    }


# ---------------------------------------------------------------------------
# run_worker
# ---------------------------------------------------------------------------

class TestRunWorker:
    @pytest.mark.asyncio
    async def test_processes_single_job_then_shuts_down(self):
        """Worker dequeues a job, processes it, then stops when queue is empty."""
        job = _make_job()
        # dequeue returns the job once, then signals shutdown via None on second call
        dequeue_results = [job, None]

        async def fake_dequeue(timeout):
            if dequeue_results:
                result = dequeue_results.pop(0)
                if result is None:
                    # Trigger shutdown after the empty dequeue
                    from app.worker import _shutdown
                    _shutdown.set()
                return result
            return None

        handle_mock = AsyncMock()

        with patch("app.worker.dequeue_review", side_effect=fake_dequeue), \
             patch("app.worker._shutdown") as shutdown_mock, \
             patch("app.main._handle_pr_event", handle_mock):

            shutdown_mock.is_set.side_effect = [False, False, True]

            from app.worker import run_worker
            await run_worker(concurrency=1)

        handle_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_respects_concurrency_limit(self):
        """No more than `concurrency` jobs run at the same time."""
        max_concurrent = 0
        current = 0

        async def slow_handler(payload, trace_id):
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            await asyncio.sleep(0.05)
            current -= 1

        jobs = [_make_job(f"delivery-{i}") for i in range(4)]
        call_count = 0

        async def fake_dequeue(timeout):
            nonlocal call_count
            if call_count < len(jobs):
                j = jobs[call_count]
                call_count += 1
                return j
            await asyncio.sleep(0.2)
            return None

        with patch("app.worker.dequeue_review", side_effect=fake_dequeue), \
             patch("app.worker._shutdown") as shutdown_mock, \
             patch("app.main._handle_pr_event", slow_handler):

            # Stop after all jobs are processed
            call_count_stop = [0]
            original_is_set = lambda: call_count >= len(jobs) and current == 0
            shutdown_mock.is_set.side_effect = lambda: call_count >= len(jobs) and current == 0

            from app.worker import run_worker
            await run_worker(concurrency=2)

        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_drains_inflight_on_shutdown(self):
        """In-flight tasks complete before worker exits."""
        completed = []

        async def slow_handler(payload, trace_id):
            await asyncio.sleep(0.05)
            completed.append(trace_id)

        job = _make_job("drain-test")
        dequeued = [False]

        async def fake_dequeue(timeout):
            if not dequeued[0]:
                dequeued[0] = True
                return job
            return None

        with patch("app.worker.dequeue_review", side_effect=fake_dequeue), \
             patch("app.worker._shutdown") as shutdown_mock, \
             patch("app.main._handle_pr_event", slow_handler):

            # Shut down immediately after dequeuing — task still in flight
            shutdown_mock.is_set.side_effect = [False, True, True, True]

            from app.worker import run_worker
            await run_worker(concurrency=1)

        assert "drain-test" in completed

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_worker(self):
        """A failing job doesn't kill the worker — next job still runs."""
        results = []

        async def flaky_handler(payload, trace_id):
            if trace_id == "fail":
                raise RuntimeError("boom")
            results.append(trace_id)

        jobs = [_make_job("fail"), _make_job("ok")]
        idx = [0]

        async def fake_dequeue(timeout):
            if idx[0] < len(jobs):
                j = jobs[idx[0]]
                idx[0] += 1
                return j
            return None

        with patch("app.worker.dequeue_review", side_effect=fake_dequeue), \
             patch("app.worker._shutdown") as shutdown_mock, \
             patch("app.main._handle_pr_event", flaky_handler):

            shutdown_mock.is_set.side_effect = lambda: idx[0] >= len(jobs) and not any(
                not t.done() for t in []
            )
            # Simple: stop after processing both
            call_count = [0]
            original = shutdown_mock.is_set

            def stop_after_two():
                call_count[0] += 1
                return call_count[0] > 6

            shutdown_mock.is_set.side_effect = stop_after_two

            from app.worker import run_worker
            await run_worker(concurrency=2)

        assert "ok" in results

    @pytest.mark.asyncio
    async def test_sets_trace_id_per_job(self):
        """Each job gets its own delivery ID set as trace ID."""
        seen_trace_ids = []

        async def capture_handler(payload, trace_id):
            seen_trace_ids.append(trace_id)

        jobs = [_make_job("trace-aaa"), _make_job("trace-bbb")]
        idx = [0]

        async def fake_dequeue(timeout):
            if idx[0] < len(jobs):
                j = jobs[idx[0]]
                idx[0] += 1
                return j
            return None

        with patch("app.worker.dequeue_review", side_effect=fake_dequeue), \
             patch("app.worker._shutdown") as shutdown_mock, \
             patch("app.main._handle_pr_event", capture_handler):

            call_count = [0]

            def stop_later():
                call_count[0] += 1
                return call_count[0] > 8

            shutdown_mock.is_set.side_effect = stop_later

            from app.worker import run_worker
            await run_worker(concurrency=2)

        assert "trace-aaa" in seen_trace_ids
        assert "trace-bbb" in seen_trace_ids
