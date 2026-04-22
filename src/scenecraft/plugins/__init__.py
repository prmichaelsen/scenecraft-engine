"""scenecraft plugins package.

Each subpackage implements one plugin via an ``activate(plugin_api)`` hook.
``api_server.run_server`` imports and registers the built-in plugins at
startup; a future dynamic loader will populate this package from a user
plugins directory without otherwise changing the contract.
"""
