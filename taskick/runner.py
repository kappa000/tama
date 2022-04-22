import importlib
import logging
import subprocess
import threading
import time
from typing import Callable

from schedule import Scheduler
from watchdog.events import FileMovedEvent
from watchdog.observers.polling import PollingObserver as Observer

from .details import ObservingDetail, TaskDetail
from .utils import (
    get_execute_command_list,
    set_a_task_to_scheduler,
    simplify_crontab_format,
)

logger = logging.getLogger("taskick")


def update_scheduler(
    scheduler: Scheduler, crontab_format: str, task: Callable, *args, **kwargs
) -> Scheduler:
    crontab_format_list = simplify_crontab_format(crontab_format)

    for crontab_format in crontab_format_list:
        scheduler = set_a_task_to_scheduler(
            scheduler, crontab_format, task, *args, **kwargs
        )

    return scheduler


def update_observer(
    observer: Observer, observe_detail: ObservingDetail, task: Callable
) -> Observer:
    handler_detail = observe_detail.handler
    event_type_detail = observe_detail.when

    EventHandlers = importlib.import_module("watchdog.events")

    if "args" in handler_detail.keys():
        handler = getattr(EventHandlers, handler_detail["name"])(
            **handler_detail["args"]
        )
    else:
        handler = getattr(EventHandlers, handler_detail["name"])()

    for event_type in event_type_detail:
        setattr(handler, f"on_{event_type}", task)

    kwargs = observe_detail.handler_args
    kwargs["event_handler"] = handler
    observer.schedule(**kwargs)

    return observer


class CommandExecuter:
    def __init__(
        self, task_name: str, command: str, propagate: bool = False, shell: bool = False
    ) -> None:
        self._task_name = task_name
        self._comand = command
        self._propagate = propagate
        self._shell = shell

    def execute_by_observer(self, event) -> None:
        logger.debug(event)
        command = self._comand
        if self._propagate:
            event_options = self._get_event_options(event)
            command = get_execute_command_list(command, event_options)

        command = " ".join(command)
        logger.debug(command)
        self.execute(command)

    def execute_by_scheduler(self) -> None:
        self.execute()

    def execute(self, command: str = None) -> None:
        if command is None:
            command = " ".join(self._comand)

        logger.info(f"Executing: {self._task_name}")
        logger.debug(f"Executing detail: {command}")
        return subprocess.Popen(command, shell=self._shell)

    def _get_event_options(self, event) -> dict:
        if isinstance(event, FileMovedEvent):
            event_keys = ["--event_type", "--src_path", "--dest_path", "--is_directory"]
            event_values = event.key
        else:
            event_keys = ["--event_type", "--src_path", "--is_directory"]
            event_values = event.key

        event_options = dict(zip(event_keys, event_values))

        if event_options["--is_directory"]:
            event_options["--is_directory"] = None
        else:
            del event_options["--is_directory"]

        return event_options

    @property
    def task_name(self):
        return self._task_name


class BaseThread(threading.Thread):
    def __init__(self, *pargs, **kwargs):
        super().__init__(daemon=True, *pargs, **kwargs)


class ThreadingScheduler(Scheduler, BaseThread):
    def __init__(self) -> None:
        Scheduler.__init__(self)
        BaseThread.__init__(self)
        self._is_active = True

    def run(self) -> None:
        while self._is_active:
            self.run_pending()
            time.sleep(1)

    def stop(self) -> None:
        self._is_active = False


class TaskRunner:
    def __init__(self) -> None:
        self._scheduler = ThreadingScheduler()
        self._observer = Observer()

        self._startup_execution_tasks = {}
        self._running_startup_tasks = {}
        self._registered_tasks = {}
        self._scheduling_tasks = {}
        self._observing_tasks = {}
        self._await_tasks = {}  # {"A": "B"} -> "A" waits for "B" to finish.

    def register(self, job_config: dict):
        TD_list = [TaskDetail(*params) for params in job_config.items()]
        for TD in TD_list:
            if not TD.is_active():
                logger.info(f"Skipped: {TD.task_name}")
                continue
            if self.is_registered(TD.task_name):
                raise ValueError(f"{TD.task_name} is already exists.")

            logger.info(f"Processing: {TD.task_name}")
            task = CommandExecuter(**TD.executor_args)

            if TD.is_startup():
                logger.info("Startup option is selected.")
                self._startup_execution_tasks[TD.task_name] = task
            if TD.is_await():
                logger.info("Await option is selected.")
                self._await_tasks[TD.task_name] = TD.await_task

            self._register(TD, task)
            self._registered_tasks[TD.task_name] = task
            logger.info("Registered")

        return self

    def run(self) -> None:
        """
        Executes registered tasks.
        Scheduled/Observed tasks will not be executed until the startup task is complete.
        """
        self._run_startup_task()
        self._observer.start()
        self._scheduler.start()

    def stop_startup_task(self):
        for proc in self._running_startup_tasks.values():
            proc.kill()

    def join_startup_task(self):
        for proc in self._running_startup_tasks.values():
            proc.wait()

    def stop(self) -> None:
        """Stop execution of registered tasks other than the startup task."""
        self.stop_startup_task()
        self._observer.stop()
        self._scheduler.stop()

    def join(self) -> None:
        self.join_startup_task()
        self._observer.join()
        self._scheduler.join()

    def _register(self, TD: TaskDetail, task: CommandExecuter) -> None:
        if TD.event_type == "time":
            self._scheduler = update_scheduler(
                self._scheduler,
                TD.when_run,
                task.execute_by_scheduler,
            )
            self._scheduling_tasks[TD.task_name] = task
        if TD.event_type == "file":
            self._observer = update_observer(
                self._observer, TD.when_run, task.execute_by_observer
            )
            self._observing_tasks[TD.task_name] = task

    def _await_running_task(self, task_name) -> None:
        for await_task_name in self._await_tasks[task_name]:
            if await_task_name not in self._running_startup_tasks.keys():
                raise ValueError(f'"{await_task_name}" is not running.')
            logger.info(f'"{task_name}" is waiting for "{await_task_name}" to finish.')
            self._running_startup_tasks[await_task_name].wait()

    def _run_startup_task(self):
        for task_name, task in self._startup_execution_tasks.items():
            if task_name in self._await_tasks.keys():
                self._await_running_task(task_name)
            self._running_startup_tasks[task_name] = task.execute()

    def is_registered(self, task_name: str) -> bool:
        return task_name in self._registered_tasks.keys()

    @property
    def scheduling_tasks(self):
        return self._scheduling_tasks

    @property
    def observing_tasks(self):
        return self._observing_tasks

    @property
    def tasks(self) -> dict:
        return self._registered_tasks

    @property
    def startup_tasks(self) -> dict:
        return self._startup_execution_tasks
