from __future__ import annotations

import threading
import json
from pathlib import Path
from dataclasses import dataclass
from time import monotonic
from typing import Any

STALE_AFTER_SECONDS = 2.0
DROPOUT_GRACE_SECONDS = 8.0
RESEARCH_DELAY_SECONDS = 3.0


@dataclass
class ChannelState:
    connected: bool = False
    device_id: int = 0
    power: int = 0
    updated_at: float = 0.0
    lost_since: float = 0.0
    restart_due_at: float = 0.0
    restarting: bool = False
    description: str = "ANT+ POWER DEVICE"
    manufacturer_id: int = 0
    model_number: int = 0


class AntPowerManager:
    """Receive ANT+ Bike Power and optionally control ANT+ FE-C trainers."""

    def __init__(
        self,
        channel_count: int = 6,
        fec_channel_count: int = 2,
    ) -> None:
        self.channel_count = channel_count
        self.fec_channel_count = fec_channel_count
        self._channels = [ChannelState() for _ in range(channel_count)]
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._node: Any = None
        self._devices: list[Any] = []
        self._power_devices: list[Any] = []
        self._fec_devices: list[Any] = []
        self._fec_devices_by_id: dict[int, Any] = {}
        self._fec_status: dict[int, str] = {}
        self._pending_resistance: dict[int, float] = {}
        self._fec_available = False
        self._running = False
        self._status = "ANT+ NOT STARTED"
        self._error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="ant-power")
        self._thread.start()

    def stop(self) -> None:
        node = self._node
        if node is not None:
            try:
                node.stop()
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        now = monotonic()
        with self._lock:
            channels = []
            restart_indexes = []
            for index, state in enumerate(self._channels):
                age_seconds = (
                    now - state.updated_at
                    if state.updated_at > 0
                    else float("inf")
                )
                fresh = (
                    state.connected
                    and age_seconds <= STALE_AFTER_SECONDS
                )
                within_dropout_grace = (
                    state.connected
                    and age_seconds <= DROPOUT_GRACE_SECONDS
                )

                if (
                    state.connected
                    and not within_dropout_grace
                    and not state.restarting
                ):
                    if state.lost_since <= 0.0:
                        state.lost_since = now
                        state.restart_due_at = (
                            now + RESEARCH_DELAY_SECONDS
                        )
                    elif now >= state.restart_due_at:
                        state.restarting = True
                        restart_indexes.append(index)
                elif within_dropout_grace:
                    state.lost_since = 0.0
                    state.restart_due_at = 0.0
                    state.restarting = False

                retry_in_seconds = (
                    max(0.0, state.restart_due_at - now)
                    if state.restart_due_at > 0.0
                    else 0.0
                )

                channels.append({
                    "connected": state.connected,
                    "device_id": state.device_id,
                    "power": (
                        state.power
                        if within_dropout_grace
                        else 0
                    ),
                    "fresh": fresh,
                    "within_dropout_grace": within_dropout_grace,
                    "age_seconds": age_seconds,
                    "restarting": state.restarting,
                    "retry_in_seconds": retry_in_seconds,
                    "description": state.description,
                    "manufacturer_id": state.manufacturer_id,
                    "model_number": state.model_number,
                })
            result = {
                "running": self._running,
                "status": self._status,
                "error": self._error,
                "channels": channels,
                "fec_available": self._fec_available,
                "fec_status": {
                    str(device_id): status
                    for device_id, status in self._fec_status.items()
                },
            }

        for index in restart_indexes:
            self._restart_power_channel_async(index)

        return result

    def _restart_power_channel_async(self, index: int) -> None:
        """
        Close and reopen a wildcard Bike Power channel after a lost signal.

        Recovery waits three seconds after LOST before this method is scheduled.
        """
        def worker() -> None:
            try:
                device = self._power_devices[index]
                channel = getattr(device, "channel", None)

                if channel is not None:
                    close_method = getattr(channel, "close", None)
                    if callable(close_method):
                        close_method()

                    open_method = getattr(channel, "open", None)
                    if callable(open_method):
                        open_method()
                    else:
                        # OpenANT device wrappers generally expose start().
                        start_method = getattr(device, "start", None)
                        if callable(start_method):
                            start_method()

                with self._lock:
                    state = self._channels[index]
                    state.connected = False
                    state.device_id = 0
                    state.power = 0
                    state.updated_at = 0.0
                    state.lost_since = 0.0
                    state.restart_due_at = 0.0
                    state.restarting = False
                    self._status = (
                        f"RESEARCHING ANT+ POWER CHANNEL {index + 1}"
                    )
            except Exception as exc:
                with self._lock:
                    state = self._channels[index]
                    state.restarting = False
                    state.lost_since = monotonic()
                    state.restart_due_at = (
                        state.lost_since + RESEARCH_DELAY_SECONDS
                    )
                    self._status = (
                        f"ANT+ RESEARCH RETRY: "
                        f"{type(exc).__name__}: {exc}"
                    )[:100]

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"ant-research-{index + 1}",
        ).start()

    def set_basic_resistance_for_device(
        self,
        device_id: int,
        resistance: float = 20.0,
    ) -> str:
        """
        Request FE-C basic-resistance mode for a selected ANT device ID.

        If the matching FE-C profile has not been discovered yet, remember the
        request and apply it immediately when that profile appears.
        """
        device_id = int(device_id or 0)
        resistance = max(0.0, min(100.0, float(resistance)))
        if device_id <= 0:
            return "NO ANT DEVICE ID"

        with self._lock:
            self._pending_resistance[device_id] = resistance
            device = self._fec_devices_by_id.get(device_id)
            if device is None:
                self._fec_status[device_id] = (
                    "FE-C SEARCHING"
                    if self._fec_available
                    else "FE-C UNAVAILABLE IN OPENANT"
                )
                return self._fec_status[device_id]

        self._send_basic_resistance_async(
            device_id,
            device,
            resistance,
        )
        return f"FE-C SETTING {resistance:.0f}%"

    def _send_basic_resistance_async(
        self,
        device_id: int,
        device: Any,
        resistance: float,
    ) -> None:
        def worker() -> None:
            try:
                device.set_basic_resistance(resistance)
                status = f"FE-C RESISTANCE {resistance:.0f}%"
            except Exception as exc:
                status = (
                    f"FE-C CONTROL FAILED: "
                    f"{type(exc).__name__}: {exc}"
                )[:100]

            with self._lock:
                self._fec_status[device_id] = status

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"fec-resistance-{device_id}",
        ).start()

    def _run(self) -> None:
        try:
            from openant.easy.node import Node
            from openant.devices import ANTPLUS_NETWORK_KEY
            from openant.devices.power_meter import PowerData, PowerMeter
            try:
                from openant.devices.fitness_equipment import (
                    FitnessEquipment,
                )
            except Exception:
                FitnessEquipment = None
        except Exception:
            with self._lock:
                self._error = "OPENANT NOT INSTALLED — RUN install_ant.bat"
            return

        try:
            with self._lock:
                self._status = "OPENING ANT USB STICK..."

            node = Node()
            self._node = node
            node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

            devices = []
            for index in range(self.channel_count):
                device = PowerMeter(node, device_id=0, name=f"power_meter_{index + 1}")

                def on_found(index=index, device=device):
                    with self._lock:
                        state = self._channels[index]
                        state.connected = True
                        state.device_id = self._read_device_id(device)
                        state.lost_since = 0.0
                        state.restart_due_at = 0.0
                        state.restarting = False
                        state.description = (
                            f"ANT+ POWER DEVICE {state.device_id}"
                            if state.device_id
                            else "ANT+ POWER DEVICE"
                        )
                        self._status = "ANT+ POWER METERS CONNECTED"

                def on_device_data(page, page_name, data, index=index, device=device):
                    with self._lock:
                        state = self._channels[index]
                        state.connected = True
                        state.device_id = (
                            state.device_id or self._read_device_id(device)
                        )

                        self._update_description(
                            state,
                            data,
                            page_name,
                        )

                        if isinstance(data, PowerData):
                            state.power = max(
                                0,
                                int(data.instantaneous_power),
                            )
                            state.updated_at = monotonic()
                            self._status = "ANT+ RECEIVING LIVE POWER"

                device.on_found = on_found
                device.on_device_data = on_device_data
                devices.append(device)

            self._power_devices = list(devices)
            self._devices = list(devices)

            fec_devices = []
            if FitnessEquipment is not None:
                self._fec_available = True
                for fec_index in range(self.fec_channel_count):
                    fec_device = FitnessEquipment(
                        node,
                        device_id=0,
                        name=f"fitness_equipment_{fec_index + 1}",
                    )

                    def on_fec_found(
                        fec_device=fec_device,
                    ):
                        device_id = self._read_device_id(fec_device)
                        if device_id <= 0:
                            return

                        with self._lock:
                            self._fec_devices_by_id[device_id] = fec_device
                            self._fec_status[device_id] = "FE-C DISCOVERED"
                            pending = self._pending_resistance.get(device_id)

                        if pending is not None:
                            self._send_basic_resistance_async(
                                device_id,
                                fec_device,
                                pending,
                            )

                    fec_device.on_found = on_fec_found
                    fec_devices.append(fec_device)

            self._fec_devices = fec_devices
            self._devices.extend(fec_devices)

            with self._lock:
                self._running = True
                self._status = (
                    f"SEARCHING ON {self.channel_count} POWER CHANNELS"
                )

            node.start()

        except Exception as exc:
            with self._lock:
                self._error = f"ANT+ ERROR: {type(exc).__name__}: {exc}"
                self._status = "CHECK ANT STICK AND WINDOWS LIBUSB DRIVER"
        finally:
            for device in self._devices:
                try:
                    device.close_channel()
                except Exception:
                    pass
            if self._node is not None:
                try:
                    self._node.stop()
                except Exception:
                    pass
            with self._lock:
                self._running = False

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        text = str(value).strip()
        if not text or text.lower() in {"none", "unknown", "n/a"}:
            return ""
        return text

    def _update_description(
        self,
        state: ChannelState,
        data: Any,
        page_name: Any,
    ) -> None:
        """
        Extract the best description broadcast by the ANT+ device.

        Some trainers transmit a human-readable product/model name. Others
        provide only manufacturer and model numbers, so this method falls back
        gracefully to those values and finally to the ANT device ID.
        """
        string_fields = (
            "product_name",
            "model_name",
            "device_name",
            "manufacturer_name",
            "name",
            "description",
        )
        number_fields = (
            ("manufacturer_id", "manufacturer_id"),
            ("manufacturer", "manufacturer_id"),
            ("model_number", "model_number"),
            ("model_id", "model_number"),
        )

        pieces: list[str] = []
        for field in string_fields:
            text = self._clean_text(getattr(data, field, None))
            if text and text not in pieces:
                pieces.append(text)

        for source_field, target_field in number_fields:
            value = getattr(data, source_field, None)
            if isinstance(value, int):
                setattr(state, target_field, value)

        # A few OpenANT data pages store useful values in dictionaries.
        if hasattr(data, "__dict__"):
            values = vars(data)
            for field in string_fields:
                text = self._clean_text(values.get(field))
                if text and text not in pieces:
                    pieces.append(text)

        if pieces:
            candidate = " ".join(pieces)[:42]
            if not candidate.lower().startswith("power_meter_"):
                state.description = candidate

        if (
            not state.description
            or state.description.upper().startswith("ANT+ POWER DEVICE")
            or state.description.upper().startswith("ANT+ TRAINER M")
        ):
            state.description = (
                f"ANT+ POWER DEVICE {state.device_id}"
                if state.device_id
                else "ANT+ POWER DEVICE"
            )

    @staticmethod
    def _read_device_id(device: Any) -> int:
        for attr in ("device_id", "_device_id"):
            value = getattr(device, attr, None)
            if isinstance(value, int):
                return value
        channel = getattr(device, "channel", None)
        if channel is not None:
            for attr in ("device_number", "_device_number"):
                value = getattr(channel, attr, None)
                if isinstance(value, int):
                    return value
        return 0
