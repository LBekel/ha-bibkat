"""Constants for the read-only Bibkat integration."""

from datetime import timedelta

DOMAIN = "bibkat"
CONF_CATALOG_ID = "catalog_id"
CONF_CATALOG_NAME = "catalog_name"
CONF_DEVICE_ID = "device_id"
UPDATE_INTERVAL = timedelta(hours=6)
