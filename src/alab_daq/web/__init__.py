"""The ALAB DAQ web server: one shared server on the DAQ computer, opened in a browser through ssh -L.

    settings.py   server settings and where its files are
    accounts.py   accounts (roles, passwords) and sign-ins
    station.py    the configuration, the run, ROOT conversion, the board check, data files, the activity log
    runs.py       the run: starts the DAQ program apart from the server and follows it
    server.py     the web layer (aiohttp): pages, the JSON interface, live events, who may do what
    wrapper.py    runs the DAQ program apart from the server, so a run goes on when the server restarts
    cli.py        python -m alab_daq.web serve | user ...
"""
