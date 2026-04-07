"""
vibrantDeck - Adjust color vibrancy of Steam Deck output
Copyright (C) 2022,2023 Sefa Eyeoglu <contact@scrumplex.net> (https://scrumplex.net)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import asyncio
import os
import sys
import struct
import subprocess
from typing import Iterable, Optional

# Takes 0.0..1.0, 0.5 being sRGB 0.5..1.0 being "boosted"
SDR_GAMUT_PROP = "GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS"
# Brightness/exposure multiplier for SDR content
SDR_INPUT_GAIN_PROP = "GAMESCOPE_SDR_INPUT_GAIN"
# Night mode for color temperature control (3 floats: amount, hue, saturation)
NIGHT_MODE_PROP = "GAMESCOPE_COLOR_NIGHT_MODE"


def float_to_long(x: float) -> int:
    return struct.unpack("!I", struct.pack("!f", x))[0]


def long_to_float(x: int) -> float:
    return struct.unpack("!f", struct.pack("!I", x))[0]


def set_cardinal_prop(prop_name: str, values: Iterable[int]):

    param = ",".join(map(str, values))

    command = ["xprop", "-root", "-f", prop_name,
               "32c", "-set", prop_name, param]

    if "DISPLAY" not in os.environ:
        command.insert(1, ":1")
        command.insert(1, "-display")

    completed = subprocess.run(command, stderr=sys.stderr, stdout=sys.stdout)

    return completed.returncode == 0


class Plugin:

    # Last values actually applied to gamescope. These are cached so that
    # we can re-apply them after resuming from suspend without requiring
    # the frontend UI to be mounted.
    _last_vibrancy: Optional[float] = None
    _last_brightness: Optional[float] = None
    _last_color_temperature: Optional[int] = None
    _last_color_intensity: Optional[float] = None

    _sleep_monitor_task: Optional[asyncio.Task] = None

    async def _main(self):
        # Valve removed SteamClient.System.RegisterForOnResumeFromSuspend
        # from the Steam Client, so plugins can no longer rely on the
        # frontend to detect resume. Instead, listen to logind's
        # PrepareForSleep signal directly from the backend.
        self._sleep_monitor_task = asyncio.create_task(self._monitor_sleep())

    async def _unload(self):
        if self._sleep_monitor_task is not None:
            self._sleep_monitor_task.cancel()
            try:
                await self._sleep_monitor_task
            except BaseException:
                pass
            self._sleep_monitor_task = None

    async def _monitor_sleep(self):
        """Watch org.freedesktop.login1 PrepareForSleep via gdbus monitor.

        gdbus monitor prints one line per signal, e.g.:
            /org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)

        When the boolean is false, the system has just resumed -- that's
        when we need to re-push our xprops, because gamescope resets them.
        """
        while True:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "gdbus", "monitor",
                    "--system",
                    "--dest", "org.freedesktop.login1",
                    "--object-path", "/org/freedesktop/login1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="ignore")
                    if "PrepareForSleep" not in line:
                        continue
                    if "(false," in line:
                        # Resumed. Re-apply with retries to outlast
                        # gamescope re-initialising its color state.
                        asyncio.create_task(self._reapply_with_retries())
                # gdbus exited; loop and relaunch after a short delay
            except asyncio.CancelledError:
                if proc is not None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                print(f"vibrantDeck: sleep monitor error: {e}", file=sys.stderr)
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await asyncio.sleep(2)

    async def _reapply_with_retries(self):
        """Re-push the last applied xprops several times after resume.

        Gamescope may not be ready the instant logind fires the resume
        signal, and it may clobber our values during its own re-init. A
        few staggered re-applications make sure our settings stick.
        """
        for delay in (0.3, 1.0, 2.5, 5.0, 8.0):
            await asyncio.sleep(delay)
            self._apply_last_values()

    def _apply_last_values(self):
        if self._last_vibrancy is not None:
            set_cardinal_prop(SDR_GAMUT_PROP,
                              [float_to_long(self._last_vibrancy)])
        if self._last_brightness is not None:
            set_cardinal_prop(SDR_INPUT_GAIN_PROP,
                              [float_to_long(self._last_brightness)])
        if (self._last_color_temperature is not None
                and self._last_color_intensity is not None):
            hue = (self._last_color_temperature / 100.0) * 0.083
            amount = self._last_color_intensity
            saturation = 1.0
            set_cardinal_prop(NIGHT_MODE_PROP, [
                float_to_long(amount),
                float_to_long(hue),
                float_to_long(saturation),
            ])

    async def set_vibrancy(self, vibrancy: float):
        vibrancy = max(vibrancy, 0.0)
        vibrancy = min(vibrancy, 2.0)

        self._last_vibrancy = vibrancy
        return set_cardinal_prop(SDR_GAMUT_PROP, [float_to_long(vibrancy)])

    async def get_vibrancy(self) -> float:
        command = ["xprop", "-root", SDR_GAMUT_PROP]

        if "DISPLAY" not in os.environ:
            command.insert(1, ":1")
            command.insert(1, "-display")

        completed = subprocess.run(command, capture_output=True)
        stdout = completed.stdout.decode("utf-8")

        # Good output: "GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS(CARDINAL) = 1065353216"
        # Bad output: "GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS:  not found."
        if "=" in stdout:

            # "1065353216"
            wideness_param = stdout.split("=")[1]
            # 1065353216
            wideness_param = int(wideness_param)
            # 1.0
            return round(long_to_float(wideness_param), 2)

        return 1.0

    async def set_brightness(self, brightness: float):
        """
        Set brightness/exposure multiplier using SDR input gain.
        brightness: 0.5 to 2.0 (represents 50% to 200%)
        """
        brightness = max(brightness, 0.5)
        brightness = min(brightness, 2.0)

        self._last_brightness = brightness
        return set_cardinal_prop(SDR_INPUT_GAIN_PROP, [float_to_long(brightness)])

    async def set_color_temperature(self, temperature: int, intensity: float):
        """
        Set color temperature shift using night mode HSV transformation.
        temperature: -100 to +100 (warm to cool)
                    -100 = red/orange shift (warm)
                    +100 = blue shift (cool)
        intensity: 0.0 to 1.0 (effect strength/amount)
        """
        # Clamp temperature to valid range
        temperature = max(-100, min(100, temperature))

        # Map temperature (-100 to +100) to hue rotation
        # We use a smaller range to avoid extreme color shifts
        # -100 = -0.083 (30° warm shift)
        # +100 = +0.083 (30° cool shift)
        hue = (temperature / 100.0) * 0.083

        # Clamp intensity
        amount = max(0.0, min(1.0, intensity))

        self._last_color_temperature = temperature
        self._last_color_intensity = amount

        # Saturation at 1.0 maintains color intensity during hue shift
        saturation = 1.0

        # Night mode expects 3 floats: [amount, hue, saturation]
        values = [
            float_to_long(amount),
            float_to_long(hue),
            float_to_long(saturation)
        ]

        return set_cardinal_prop(NIGHT_MODE_PROP, values)
