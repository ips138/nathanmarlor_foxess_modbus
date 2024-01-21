from ..const import H1_SET
from .modbus_remote_control_config import ModbusRemoteControlAddressConfig
from .modbus_remote_control_config import ModbusRemoteControlFactory
from .modbus_remote_control_config import RemoteControlAddressSpec

REMOTE_CONTROL_DESCRIPTION = ModbusRemoteControlFactory(
    addresses=[
        RemoteControlAddressSpec(
            H1_SET,
            input=ModbusRemoteControlAddressConfig(
                remote_enable=44000,
                timeout_set=44001,
                active_power=44002,
                work_mode=41000,
                battery_soc=11036,
                max_soc=41010,
                load_power=11023,
                inverter_power=11011,
                pv_power_limit=44013,
                pv_powers=[11002, 11005],
            ),
        )
    ]
)
