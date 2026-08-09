# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Publish aviation_feeder (readsb) feeder-health metrics to Home Assistant
over MQTT, on a single asyncio loop via aiomqtt."""

import os

__version__ = os.environ.get("ADDON_VERSION", "dev")
