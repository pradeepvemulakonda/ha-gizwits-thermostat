"""Constants for the Radiant Wi-Fi Thermostat integration."""

DOMAIN = "gizwits_thermostat"

CONF_DID = "did"
CONF_MAC = "mac"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 300  # seconds; safety-net poll. State changes normally arrive instantly by UDP push.
MIN_SCAN_INTERVAL = 30        # local status reads are fast now (a single request/reply)

SERVICE_SEND_RAW = "send_raw"
