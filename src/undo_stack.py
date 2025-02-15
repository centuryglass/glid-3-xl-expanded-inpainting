"""Global stack for tracking undo/redo state."""
import logging
from contextlib import contextmanager
import datetime
from threading import Lock
from typing import Callable, Optional, Any, Generator

from PySide6.QtCore import QObject, Signal

from src.config.application_config import AppConfig
from src.util.singleton import Singleton

logger = logging.getLogger(__name__)
MAX_UNDO = 50


class _UndoAction:
    def __init__(self, undo_action: Callable[[], None],
                 redo_action: Callable[[], None],
                 action_type: str,
                 action_data: Optional[dict[str, Any]]) -> None:
        self.undo = undo_action
        self.redo = redo_action
        self.type = action_type
        self.action_data = action_data
        self.timestamp = datetime.datetime.now().timestamp()


class _UndoGroup:
    def __init__(self, action_type: str) -> None:
        self.type = action_type
        self.timestamp = datetime.datetime.now().timestamp()
        self._undo_actions: list[Callable[[], None]] = []
        self._redo_actions: list[Callable[[], None]] = []

    def add_to_group(self, undo_action: Callable[[], None], redo_action: Callable[[], None], action_type: str):
        """Add a new action to the set of grouped actions."""
        self._undo_actions.insert(0, undo_action)
        self._redo_actions.append(redo_action)
        self.timestamp = datetime.datetime.now().timestamp()
        if action_type not in self.type:
            self.type = f'{self.type},{action_type}'

    def undo(self):
        """Undo all actions in the group."""
        for action in self._undo_actions:
            action()

    def redo(self):
        """Redo all actions in the group."""
        for action in self._redo_actions:
            action()

    def count(self) -> int:
        """Return the number of actions in the group"""
        return len(self._undo_actions)


class UndoStack(metaclass=Singleton):
    """Manages the application's shared undo history."""

    def __init__(self) -> None:
        self._undo_stack: list[_UndoAction | _UndoGroup] = []
        self._redo_stack: list[_UndoAction | _UndoGroup] = []
        self._access_lock = Lock()
        self._open_group: Optional[_UndoGroup] = None
        self._in_progress_change = 'none'
        self._undo_in_progress = False

        class _SignalManager(QObject):
            undo_count_changed = Signal(int)
            redo_count_changed = Signal(int)
        self._signal_manager = _SignalManager()

    @property
    def undo_in_progress(self) -> bool:
        """Returns whether an undo action is currently in progress."""
        return self._undo_in_progress

    @property
    def undo_count_changed(self) -> Signal:
        """Returns the signal emitted whenever undo action count changes."""
        return self._signal_manager.undo_count_changed

    @property
    def redo_count_changed(self) -> Signal:
        """Returns the signal emitted whenever redo action count changes."""
        return self._signal_manager.redo_count_changed

    def undo_count(self) -> int:
        """Returns the number of saved actions in the undo stack."""
        return len(self._undo_stack)

    def redo_count(self) -> int:
        """Returns the number of saved actions in the redo stack."""
        return len(self._redo_stack)

    def commit_action(self, action: Callable[[], None], undo_action: Callable[[], None], action_type: str,
                      action_data: Optional[dict[str, Any]] = None, skip_initial_call=False) -> bool:
        """Performs an action, then commits it to the undo stack.

        The undo stack is lock-protected.  Make sure that the function parameters provided don't also call
         commit_action.

        Parameters
        ----------
        action: Callable
            Some action function to run, accepting zero parameters.
        undo_action: Callable
            A function that completely reverses the changes caused by the `action` function.

            These parameters should be designed to leave the application in the same state if the following code runs,
            for any value n:
            ```
            for i in range(n):
                action()
                undo_action()
        action_type: str
            An arbitrary label used to identify the action, to be used when attempting to merge actions in the stack.
        action_data: dict
            Arbitrary data to use for merging actions.
        skip_initial_call: bool, default=False
            If true, skip the initial action() call.
        """
        if self._access_lock.locked():
            raise RuntimeError(f'Concurrent undo history changes detected! Attempted: {action_type}, '
                               f'in-progress: {self._in_progress_change}')
        with self._access_lock:
            logger.info(f'ADD ACTION:{action_type}, UNDO_COUNT={len(self._undo_stack)},'
                        f' REDO_COUNT={len(self._redo_stack)}')
            self._in_progress_change = action_type
            if not skip_initial_call:
                action()
            if self._open_group is not None:
                self._open_group.add_to_group(undo_action, action, action_type)
            else:
                undo_entry = _UndoAction(undo_action, action, action_type, action_data)
                prev_action = None if len(self._undo_stack) == 0 else self._undo_stack[-1]
                if prev_action is not None and undo_entry.timestamp - prev_action.timestamp \
                        < AppConfig().get(AppConfig.UNDO_MERGE_INTERVAL):
                    if isinstance(prev_action, _UndoGroup):
                        prev_action.add_to_group(undo_action, action, action_type)
                    else:
                        self._undo_stack.remove(prev_action)
                        new_group = _UndoGroup(prev_action.type)
                        new_group.add_to_group(prev_action.undo, prev_action.redo, prev_action.type)
                        new_group.add_to_group(undo_action, action, action_type)
                        self._add_to_stack(new_group, self._undo_stack)
                else:
                    self._add_to_stack(undo_entry, self._undo_stack)
            if len(self._redo_stack) > 0:
                self._redo_stack.clear()
                self.redo_count_changed.emit(0)
            self._in_progress_change = 'none'
        return True

    @contextmanager
    def last_action(self, action_type: str) -> Generator[Optional[_UndoAction | _UndoGroup], None, None]:
        """Access the most recent action, potentially updating it to combine actions."""
        if self._access_lock.locked():
            raise RuntimeError(f'Concurrent undo history changes detected! Attempted: {action_type}, '
                               f'in-progress: {self._in_progress_change}')
        with self._access_lock:
            yield None if len(self._undo_stack) == 0 else self._undo_stack[-1]

    @contextmanager
    def combining_actions(self, action_type: str) -> Generator[None, None, None]:
        """Combines all actions added with commit_action until the context is exited."""
        if self._access_lock.locked():
            raise RuntimeError(f'Concurrent undo history changes detected! Attempted: {action_type}, '
                               f'in-progress: {self._in_progress_change}')
        with self._access_lock:
            if self._open_group is not None:
                raise RuntimeError(f'Tried to create {action_type} combined action group, but '
                                   f'{self._open_group.type} group is still open')
            self._open_group = _UndoGroup(action_type)
        assert not self._access_lock.locked()
        yield
        if self._access_lock.locked():
            raise RuntimeError(f'Concurrent undo history changes detected! Attempted: {action_type}, '
                               f'in-progress: {self._in_progress_change}')
        with self._access_lock:
            assert self._open_group is not None and self._open_group.type.startswith(action_type)
            if self._open_group.count() > 0:
                self._add_to_stack(self._open_group, self._undo_stack)
            self._open_group = None

    def undo(self) -> None:
        """Reverses the most recent action taken."""
        with self._access_lock:
            self._undo_in_progress = True
            if len(self._undo_stack) == 0:
                return
            last_action_object = self._undo_stack.pop()
            logger.info(f'UNDO ACTION:{last_action_object.type}, UNDO_COUNT={len(self._undo_stack)},'
                        f' REDO_COUNT={len(self._redo_stack)}')
            last_action_object.undo()
            self.undo_count_changed.emit(len(self._undo_stack))
            self._add_to_stack(last_action_object, self._redo_stack)
            self._undo_in_progress = False

    def redo(self) -> None:
        """Re-applies the last undone action as long as no new actions were registered after the last undo."""
        with self._access_lock:
            if len(self._redo_stack) == 0:
                return
            last_action_object = self._redo_stack.pop()
            logger.info(f'REDO ACTION:{last_action_object.type}, UNDO_COUNT={len(self._undo_stack)},'
                        f' REDO_COUNT={len(self._redo_stack)}')
            last_action_object.redo()
            self.redo_count_changed.emit(len(self._redo_stack))
            self._add_to_stack(last_action_object, self._undo_stack)

    def clear(self) -> None:
        """Clears the entire undo/redo history."""
        with self._access_lock:
            assert self._open_group is None
            undo_count = self.undo_count()
            redo_count = self.redo_count()
            self._undo_stack.clear()
            self._redo_stack.clear()
            if undo_count != 0:
                self.undo_count_changed.emit(0)
            if redo_count != 0:
                self.redo_count_changed.emit(0)

    def _add_to_stack(self, stack_item: _UndoAction | _UndoGroup, stack: list[_UndoAction | _UndoGroup]) -> None:
        if stack == self._undo_stack:
            stack_signal = self.undo_count_changed
        else:
            assert stack == self._redo_stack
            stack_signal = self.redo_count_changed
        stack.append(stack_item)
        if len(stack) > MAX_UNDO:
            if len(stack) != (MAX_UNDO + 1):
                stack_signal.emit(MAX_UNDO)
            while len(stack) > MAX_UNDO:
                stack.pop(0)
        else:
            stack_signal.emit(len(stack))
