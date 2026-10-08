"""Abstract controller backend interface."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controllers.events import ControllerEvent


class ControllerBackend(abc.ABC):
    """A source of semantic controller events."""

    @abc.abstractmethod
    def poll(self) -> ControllerEvent | None:
        """Return the next pending event, or None if no event is available."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release any resources held by the backend."""

    def __enter__(self) -> ControllerBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
