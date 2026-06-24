"""Data.py - Shared in-process state for the Caribou System Unit.

A single `Data` instance is constructed by CSU.py at startup and passed
to each worker. Workers acquire `tlock` before reading or writing.

Three structured dicts mirror the Caribou Hub telemetry spec §4 so
HubLink can pass them through with no translation:

  MAVLinkPacket  -> flight-controller telemetry (populated by MAVLink.py)
  BMSArms        -> per-arm DroneCAN BatteryInfo (populated by TattuBMS.py)
  ESCArms        -> per-arm DroneCAN esc.Status   (populated by Hobbywing.py)
"""

import threading


class Data:
    def __init__(self) -> None:
        self.tlock = threading.Lock()

        # MAVLink/ArduPilot telemetry — shape matches Caribou Hub telemetry §4.
        self.MAVLinkPacket = {
            'attitude': None,             # {roll_deg, pitch_deg, yaw_deg, timestamp}
            'position': None,             # {latitude_deg, longitude_deg, absolute_altitude_m, relative_altitude_m, timestamp}
            'gps': None,                  # {num_satellites, fix_type, timestamp}
            'battery_fc': None,           # {voltage_v, remaining_percent, timestamp}
            'in_air': None,               # bool
            'flight_mode': None,          # str (e.g. "LOITER", "AUTO")
            'airspeed_ms': None,          # float (ground-speed magnitude, m/s)
            'vertical_speed_ms': None,    # float (m/s, positive = climbing)
            'heading_deg': None,          # float (0..360)
        }

        # Per-arm BMS state, keyed by arm_id 1..6. Absence of a key means we
        # haven't heard from that BMS yet — HubLink omits the bms block then.
        self.BMSArms: dict[int, dict] = {}
        # {arm_id: {voltage_v, current_a, temperature_c, soc_pct, soh_pct, timestamp}}

        # Per-arm ESC state, keyed by arm_id 1..6. Same shape semantics as BMSArms.
        self.ESCArms: dict[int, dict] = {}
        # {arm_id: {rpm, voltage_v, current_a, temperature_c, motor_temperature_c, timestamp}}
