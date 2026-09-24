from typing import Any


async def getStatus(*, homey, query, params, body) -> Any:
    return await homey.app.api_get_status()


async def connectTailscale(*, homey, query, params, body) -> Any:
    return await homey.app.api_connect()


async def disconnectTailscale(*, homey, query, params, body) -> Any:
    return await homey.app.api_disconnect()


async def reconnectTailscale(*, homey, query, params, body) -> Any:
    return await homey.app.api_reconnect()


async def refreshTailscale(*, homey, query, params, body) -> Any:
    return await homey.app.api_refresh()