"""nowplaying - track now-playing dashboard (Bluetooth + local players).

- ``nowplaying.monitor`` - the D-Bus producer (BlueZ + MPRIS, ``main()``).
- ``nowplaying.web``   - the FastAPI consumer / web server.
- ``nowplaying.config`` / ``nowplaying.db`` - configuration and persistence.
"""