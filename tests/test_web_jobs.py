import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mra.web_app import create_app
from mra.web_jobs import AnalysisJob, JobManager


class JobManagerShutdownTests(unittest.TestCase):
    def test_shutdown_cancels_pending_work_and_stops_active_process(self):
        manager = JobManager(Path.cwd())
        queued = AnalysisJob("queued", Path.cwd(), 5, False)
        queued.status = "queued"
        queued.future = mock.Mock()
        queued.future.cancel.return_value = True

        running = AnalysisJob("running", Path.cwd(), 5, False)
        running.status = "running"
        running.future = mock.Mock()
        running.future.cancel.return_value = False
        running.process = mock.Mock()
        running.process.poll.return_value = None

        manager._jobs = {queued.id: queued, running.id: running}
        with mock.patch.object(manager, "_stop_process_tree") as stop_process, \
             mock.patch.object(manager._executor, "shutdown") as shutdown_executor:
            manager.shutdown()
            manager.shutdown()

        self.assertTrue(queued.cancel_requested)
        self.assertEqual(queued.status, "cancelled")
        self.assertIsNotNone(queued.finished_at)
        self.assertTrue(running.cancel_requested)
        stop_process.assert_called_once_with(running.process)
        shutdown_executor.assert_called_once_with(wait=True, cancel_futures=True)

    def test_submit_is_rejected_after_shutdown(self):
        manager = JobManager(Path.cwd())
        manager.shutdown()

        with self.assertRaisesRegex(RuntimeError, "已经停止"):
            manager.submit(Path.cwd(), 5)

    def test_cancel_stops_process_that_was_still_starting(self):
        manager = JobManager(Path.cwd())
        popen_entered = threading.Event()
        allow_popen_to_return = threading.Event()
        process = mock.Mock()
        process.stdout = []
        process.poll.return_value = None
        process.wait.return_value = 0

        def delayed_popen(*_args, **_kwargs):
            popen_entered.set()
            self.assertTrue(allow_popen_to_return.wait(timeout=2))
            return process

        with mock.patch("mra.web_jobs.subprocess.Popen", delayed_popen), \
             mock.patch.object(manager, "_stop_process_tree") as stop_process:
            job = manager.submit(Path.cwd(), 5)
            self.assertTrue(popen_entered.wait(timeout=2))
            manager.cancel(job.id)
            allow_popen_to_return.set()
            job.future.result(timeout=2)

        self.assertEqual(job.status, "cancelled")
        stop_process.assert_called_once_with(process)
        manager.shutdown()

    def test_fastapi_lifespan_shuts_down_job_manager(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "songs"
            app = create_app(songs_root=root)
            with mock.patch.object(app.state.jobs, "shutdown") as shutdown:
                async def exercise_lifespan():
                    async with app.router.lifespan_context(app):
                        pass

                asyncio.run(exercise_lifespan())

        shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
