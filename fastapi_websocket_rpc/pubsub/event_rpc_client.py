import functools
import asyncio
from typing import Coroutine, List
from tenacity import retry, wait
from websockets.exceptions import WebSocketException, ConnectionClosed

from ..logger import get_logger
from .event_notifier import Topic
from ..websocket.rpc_methods import RpcMethodsBase
from ..websocket.websocket_rpc_client import WebSocketRpcClient
from .event_notifier import Subscription, Topic
from .rpc_event_methods import RpcEventClientMethods

logger = get_logger('RpcClient')

def apply_retry(func):
    @functools.wraps(func)
    async def wrapped_with_retries(self, *args, **kwargs):
        if self._retry_config is False:
            new_func = func
        else:
            retry_decorator = retry(**self._retry_config)
            new_func = retry_decorator(func)
        return await new_func(self, *args, **kwargs)
    return wrapped_with_retries

class EventRpcClient:
    """
    RPC pub/sub client

    Simple usage example (init class with subscription topics):
        client = EventRpcClient(["guns", "germs", "steel"])
        client.start_client("ws://localhost:8000/ws/test-client1")

    If you want to run callbacks on topic events:
        client = EventRpcClient()
        # guns_coroutine will be awaited on when event arrives on "guns" topic
        client.subscribe("guns", guns_coroutine)
        client.subscribe("germs", germs_coroutine)

    you can also run callback on successful connection
        client.on_connect(on_connect_coroutine)

    when you are done registering callbacks, call (once you do, you cannot subscribe to more topics)
    client.start_client("ws://localhost:8000/ws/test-client1")

    Advanced usage:
        override on_connect() to add more subscription / registartion logic
    """

    def __init__(self, topics: List[Topic] = [], methods_class=None, retry_config=None, **kwargs) -> None:
        """
        Args:
            topics (List[Topic]): topics client should subscribe to.
            methods_class ([RpcMethodsBase], optional): RPC Methods exposed by client. Defaults to RpcEventClientMethods.
            retry_config (Dict, optional): Tenacity (https://tenacity.readthedocs.io/) retry kwargs. Defaults to  {'wait': wait.wait_random_exponential(max=45)}
                                           retry_config is used both for initial connection failures and reconnects upon connection loss
        """
        self._methods = methods_class(self) if methods_class is not None else RpcEventClientMethods(self)
        self._topics = topics # these topics will not have an attached callback
        self._callbacks = {}
        self._on_connect_callbacks = []
        self._running = False
        self._connect_kwargs = kwargs
        # Tenacity retry configuration
        self._retry_config = retry_config if retry_config is not None else {'wait': wait.wait_random_exponential(max=45)}

    @apply_retry
    async def run(self, uri, wait_on_reader=True):
        """
        runs the rpc client (async api).
        if you want to call from a synchronous program, use start_client().
        """
        logger.info("trying to connect", server_uri=uri)
        async with WebSocketRpcClient(uri, self._methods, retry_config=self._retry_config, **self._connect_kwargs) as client:
            try:
                # if we managed to connect
                if client is not None:
                    self._running = True
                    await self._on_connection(client)
                    if wait_on_reader:
                        await client.wait_on_reader()
            except ConnectionClosed:
                logger.error("RPC connection lost")
                # re-Raise so retry can reconnect us
                raise
            except WebSocketException as err:
                logger.info("RPC connection failed", error=err)
                # re-Raise so retry can reconnect us
                raise
            except Exception as err:
                logger.critical("RPC Uncaught Error", error=err)
                # re-Raise so retry can reconnect us
                raise
            finally:
                self._running = False

    def subscribe(self, topic: Topic, callback: Coroutine):
        if not self._running:
            self._topics.append(topic)
            self._callbacks[topic] = callback

    def on_connect(self, callback: Coroutine):
        self._on_connect_callbacks.append(callback)

    async def _on_connection(self, client):
        """
        Method called upon first connection to server
        """
        logger.info(f"connected to server", server_uri=client.uri)
        if self._topics:
            await client.channel.other.subscribe(topics=self._topics)
        if self._on_connect_callbacks:
            await asyncio.gather(*(callback() for callback in self._on_connect_callbacks))

    async def act_on_topic(self, topic: Topic, data=None):
        if topic in self._callbacks:
            await self._callbacks[topic](data=data)

    def start_client(self, server_uri, loop: asyncio.AbstractEventLoop = None, run_sync=True):
        """
        Start the client and wait [if run_sync=True] on the sever-side

        Args:
            server_uri ([type]): uri to server
            loop (asyncio.AbstractEventLoop, optional): even loop to run on. Defaults to asyncio.get_event_loop().
            run_sync (bool, optional): Wait on server. Defaults to True.
            Defaults to {}.
        """
        loop = loop or asyncio.get_event_loop()
        loop.run_until_complete(self.run(server_uri, run_sync))

    def start_client_async(self, server_uri, loop: asyncio.AbstractEventLoop = None):
        """
        Start the client and return once finished subscribing to events
        RPC notifications will still be handeled in the background
        """
        self.start_client(server_uri, loop, False)


