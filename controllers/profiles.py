"""Controller profile loader and default mapping."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from controllers.events import (
    ButtonEvent,
    ButtonOp,
    ControlKey,
    ControllerEvent,
    EncoderEvent,
    FaderEvent,
    JogEvent,
)

MIDI_CC_MAX = 127
ENCODER_CENTER_VALUE = 64
PITCH_BEND_CENTER = 0x2000


@dataclass(frozen=True)
class ButtonMapping:
    """Mapping from a MIDI note to a firmware button key."""

    key: ControlKey
    channel: int = 0


@dataclass(frozen=True)
class ContinuousMapping:
    """Mapping from a MIDI CC to a fader or encoder key."""

    key: ControlKey
    channel: int = 0
    analog_scale: tuple[float, float] = (-1.0, 1.0)


@dataclass(frozen=True)
class JogMapping:
    """Mapping from MIDI pitch-bend to a jog key."""

    key: ControlKey
    channel: int = 0


class Profile:
    """A device profile that translates MIDI messages into semantic events."""

    def __init__(
        self,
        name: str,
        buttons: dict[int, ButtonMapping],
        faders: dict[int, ContinuousMapping],
        encoders: dict[int, ContinuousMapping],
        jog: JogMapping | None,
    ) -> None:
        self.name = name
        self.buttons = buttons
        self.faders = faders
        self.encoders = encoders
        self.jog = jog

    def map_note(self, note: int, midi_channel: int, velocity: int) -> ButtonEvent | None:
        """Return a button press/release event for a MIDI note message."""
        mapping = self.buttons.get(note)
        if mapping is None:
            return None
        op = ButtonOp.PRESS if velocity > 0 else ButtonOp.RELEASE
        channel = mapping.channel if mapping.channel >= 0 else midi_channel
        return ButtonEvent(key=mapping.key.value, op=op, channel=channel)

    def map_cc(self, cc: int, value: int, midi_channel: int) -> ControllerEvent | None:
        """Return a fader or encoder event for a MIDI CC message."""
        fader = self.faders.get(cc)
        if fader is not None:
            low, high = fader.analog_scale
            analog = low + (value / float(MIDI_CC_MAX)) * (high - low)
            channel = fader.channel if fader.channel >= 0 else midi_channel
            return FaderEvent(key=fader.key.value, value=value, channel=channel, analog=analog)
        encoder = self.encoders.get(cc)
        if encoder is not None:
            # Treat values 64..127 as clockwise ticks, 63..1 as counter-clockwise.
            if value == 0 or value == ENCODER_CENTER_VALUE:
                delta = 0
            elif value > ENCODER_CENTER_VALUE:
                delta = value - ENCODER_CENTER_VALUE
            else:
                delta = value
            channel = encoder.channel if encoder.channel >= 0 else midi_channel
            return EncoderEvent(key=encoder.key.value, delta=delta, channel=channel)
        return None

    def map_pitch_bend(self, value: int, midi_channel: int) -> JogEvent | None:
        """Return a jog event for a MIDI pitch-bend message."""
        if self.jog is None:
            return None
        # 14-bit signed value; convert to signed int.
        signed = value - PITCH_BEND_CENTER
        channel = self.jog.channel if self.jog.channel >= 0 else midi_channel
        return JogEvent(key=self.jog.key.value, delta=signed, channel=channel)


def _normalize_key(value: Any) -> ControlKey:
    """Convert a profile key value to a ControlKey enum."""
    if isinstance(value, int):
        return ControlKey(value)
    if isinstance(value, str):
        name = re.sub(r"[^A-Z0-9]", "", value.upper())
        try:
            return ControlKey[name]
        except KeyError as exc:
            raise ValueError(f"Unknown control key: {value!r}") from exc
    raise TypeError(f"Key must be int or str, got {type(value)}")


def _parse_analog_scale(value: Any, control: str) -> tuple[float, float]:
    """Validate and normalize the low and high analog scale values."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{control} analog_scale must contain exactly two numbers")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError(f"{control} analog_scale must contain exactly two numbers")
    try:
        scale = float(value[0]), float(value[1])
    except OverflowError as exc:
        raise ValueError(f"{control} analog_scale values must be finite") from exc
    if not all(math.isfinite(item) for item in scale):
        raise ValueError(f"{control} analog_scale values must be finite")
    return scale


DEFAULT_PROFILE = Profile(
    name="default",
    buttons={
        36: ButtonMapping(key=ControlKey.PLAY, channel=0),
        37: ButtonMapping(key=ControlKey.LOAD, channel=0),
        38: ButtonMapping(key=ControlKey.SOURCE, channel=0),
    },
    faders={
        1: ContinuousMapping(key=ControlKey.TEMPO, channel=0, analog_scale=(-1.0, 1.0)),
    },
    encoders={
        2: ContinuousMapping(key=ControlKey.BROWSE, channel=0),
    },
    jog=JogMapping(key=ControlKey.JOG, channel=0),
)


def load_profile(path: str | Path | None = None) -> Profile:
    """Load a profile from JSON, or return the default profile if path is None."""
    if path is None:
        return DEFAULT_PROFILE
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    buttons = {}
    for note, raw in data.get("buttons", {}).items():
        mapping = ButtonMapping(
            key=_normalize_key(raw.get("key")),
            channel=raw.get("channel", 0),
        )
        buttons[int(note)] = mapping
    faders = {}
    for cc, raw in data.get("faders", {}).items():
        analog_scale = _parse_analog_scale(raw.get("analog_scale", [-1.0, 1.0]), f"Fader CC {cc}")
        faders[int(cc)] = ContinuousMapping(
            key=_normalize_key(raw.get("key")),
            channel=raw.get("channel", 0),
            analog_scale=analog_scale,
        )
    encoders = {}
    for cc, raw in data.get("encoders", {}).items():
        encoders[int(cc)] = ContinuousMapping(
            key=_normalize_key(raw.get("key")),
            channel=raw.get("channel", 0),
        )
    jog = None
    raw_jog = data.get("jog")
    if raw_jog:
        jog = JogMapping(
            key=_normalize_key(raw_jog.get("key")),
            channel=raw_jog.get("channel", 0),
        )
    return Profile(
        name=data.get("name", "custom"),
        buttons=buttons,
        faders=faders,
        encoders=encoders,
        jog=jog,
    )
