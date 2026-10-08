"""Semantic controller events and bridge serialization."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum


class ButtonOp(IntEnum):
    """Operations supported by the firmware control device."""

    PRESS = 0
    RELEASE = 2


class ControlKey(IntEnum):
    """Known firmware control keys from the XDJ-RX3 control adapter."""

    SOURCE = 0x201
    BROWSE = 0x420C
    LOAD = 0x4311
    PLAY = 0x4101
    JOG = 0x4305
    TEMPO = 0x4401


@dataclass(frozen=True)
class ButtonEvent:
    """A physical button press or release."""

    key: int
    op: ButtonOp
    channel: int = 0
    value: int = 0
    analog: float = 0.0


@dataclass(frozen=True)
class JogEvent:
    """A jog wheel tick or zero report."""

    key: int
    delta: int
    channel: int = 0


@dataclass(frozen=True)
class FaderEvent:
    """A continuous fader or knob with a 0..127 (or scaled) value."""

    key: int
    value: int
    channel: int = 0
    analog: float = 0.0


@dataclass(frozen=True)
class EncoderEvent:
    """A rotary encoder tick."""

    key: int
    delta: int
    channel: int = 0


ControllerEvent = ButtonEvent | JogEvent | FaderEvent | EncoderEvent


def serialize_event(event: ControllerEvent) -> bytes:
    """Convert an event to a 0x30 bridge payload.

    The firmware expects: key i32, op i32, channel i32, value i32, analog f32.
    """
    if isinstance(event, ButtonEvent):
        return struct.pack("<iiiif", event.key, event.op, event.channel, event.value, event.analog)
    if isinstance(event, JogEvent):
        # Jog ticks use op 4 with the tick count in value.
        return struct.pack("<iiiif", event.key, 4, event.channel, event.delta, 0.0)
    if isinstance(event, FaderEvent):
        # Faders/tempo use op 5 with the analog value in f32.
        return struct.pack("<iiiif", event.key, 5, event.channel, event.value, event.analog)
    if isinstance(event, EncoderEvent):
        # Encoders are treated like jog ticks without analog output.
        return struct.pack("<iiiif", event.key, 4, event.channel, event.delta, 0.0)
    raise TypeError(f"Unsupported event type: {type(event)}")
