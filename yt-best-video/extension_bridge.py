import asyncio
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets


class ExtensionBridgeServer:
    def __init__(self, host, control_port, template_port):
        self.host = host
        self.control_port = control_port
        self.template_port = template_port
        self._control_clients = set()
        self._template_clients = set()
        self._loop = None
        self._thread = None
        self._control_server = None
        self._template_server = None
        self._ready_event = threading.Event()
        self._running = False
        self._last_error = None
        self._latest_payload = None
        self._lock = threading.Lock()
        self._pending_control_requests = {}
        self._pending_template_requests = {}
        self._control_connected_event = threading.Event()
        self._template_connected_event = threading.Event()
        self._journeys_updated_event = threading.Event()
        self._template_sync_event = threading.Event()
        self._chatgpt_tab_ready_event = threading.Event()
        self._execution_validation_event = threading.Event()
        self._journey_status = {}
        self._journey_completion_events = {}
        self._journeys = []
        self._latest_template_ack = None
        self._latest_chatgpt_tab_status = None
        self._latest_execution_validation = None
        self._latest_execution_id_by_journey = {}
        self._latest_journeys_request_id = None

    @property
    def last_error(self):
        return self._last_error

    def get_connection_state(self):
        return {
            "control_connected": self._control_connected_event.is_set(),
            "template_connected": self._template_connected_event.is_set(),
            "control_clients": len(self._control_clients),
            "template_clients": len(self._template_clients),
        }

    def is_control_connected(self):
        return self._control_connected_event.is_set()

    def is_template_connected(self):
        return self._template_connected_event.is_set()

    def wait_for_connections(self, timeout=15.0):
        if not self._control_connected_event.wait(timeout):
            return False, "La extension web no se conecto al canal principal (8765)."

        if not self._template_connected_event.wait(timeout):
            return False, "La extension web no se conecto al bridge de variables (8766)."

        return True, None

    def get_available_journeys(self):
        with self._lock:
            return list(self._journeys)

    def get_journey_status(self, journey_id):
        with self._lock:
            status = self._journey_status.get(journey_id)
            if not status:
                return None
            return dict(status)

    def get_latest_execution_id(self, journey_id):
        with self._lock:
            return self._latest_execution_id_by_journey.get(journey_id)

    def _new_request_id(self, prefix="req"):
        return f"{prefix}-{uuid.uuid4()}"

    def _new_execution_id(self):
        return self._new_request_id("exec")

    def _register_pending_request(self, scope, request_id):
        pending = {
            "event": threading.Event(),
            "payload": None,
        }
        scope[request_id] = pending
        return pending

    def _resolve_pending_request(self, scope, request_id, payload):
        if not request_id:
            return False

        pending = scope.get(request_id)
        if not pending:
            return False

        pending["payload"] = payload
        pending["event"].set()
        return True

    def _wait_pending_request(self, scope, request_id, timeout, timeout_message):
        pending = scope.get(request_id)
        if not pending:
            return False, timeout_message, None

        if not pending["event"].wait(timeout):
            scope.pop(request_id, None)
            return False, timeout_message, None

        payload = pending.get("payload")
        scope.pop(request_id, None)
        return True, None, payload

    def start(self, timeout=1.5):
        if self._thread and self._thread.is_alive():
            return self._running

        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout)
        return self._running

    def stop(self):
        if not self._loop or not self._loop.is_running():
            return

        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            future.result(timeout=2)
        except Exception:
            pass

    def _start_heartbeat(self):
        async def _heartbeat_loop():
            while self._running:
                try:
                    await asyncio.sleep(20)
                    if self._control_clients:
                        await self._broadcast(
                            self._control_clients,
                            {
                                "action": "HEARTBEAT",
                                "ts": datetime.now(timezone.utc).timestamp(),
                            },
                        )
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_heartbeat_loop(), self._loop)

    def request_journeys(self):
        if not self._running or not self._loop:
            return False, self._last_error or "El servidor WebSocket no esta corriendo."

        if not self.is_control_connected():
            return False, "La extension no esta conectada al canal principal."

        self._journeys_updated_event.clear()
        request_id = self._new_request_id("journeys")
        self._latest_journeys_request_id = request_id
        self._register_pending_request(self._pending_control_requests, request_id)

        payload = {"action": "GET_JOURNEYS", "request_id": request_id}
        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(self._control_clients, payload),
            self._loop,
        )
        try:
            future.result(timeout=2)
        except Exception as exc:
            self._pending_control_requests.pop(request_id, None)
            self._last_error = str(exc)
            return False, self._last_error

        return True, None

    def wait_for_journeys(self, timeout=4.0):
        request_id = self._latest_journeys_request_id
        if request_id:
            ok, error, _payload = self._wait_pending_request(
                self._pending_control_requests,
                request_id,
                timeout,
                "La extension no devolvio la lista de journeys a tiempo.",
            )
            self._latest_journeys_request_id = None
            if ok:
                return True, None
            return False, error

        if self._journeys_updated_event.wait(timeout):
            return True, None
        return False, "La extension no devolvio la lista de journeys a tiempo."

    def prepare_chatgpt_tab(self, timeout=15.0, tab_url_patterns=None):
        if not self._running or not self._loop:
            return False, self._last_error or "El servidor WebSocket no esta corriendo."

        if not self.is_control_connected():
            return False, "La extension no esta conectada al canal principal."

        self._chatgpt_tab_ready_event.clear()
        self._latest_chatgpt_tab_status = None
        request_id = self._new_request_id("prepare-tab")
        self._register_pending_request(self._pending_control_requests, request_id)

        payload = {"action": "PREPARE_CHATGPT_TAB", "request_id": request_id}
        if tab_url_patterns:
            payload["tab_url_patterns"] = tab_url_patterns

        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(self._control_clients, payload),
            self._loop,
        )
        try:
            future.result(timeout=2)
        except Exception as exc:
            self._pending_control_requests.pop(request_id, None)
            self._last_error = str(exc)
            return False, self._last_error

        ok, error, status = self._wait_pending_request(
            self._pending_control_requests,
            request_id,
            timeout,
            "La extension no preparo una pestaña de ChatGPT a tiempo.",
        )
        if not ok:
            return False, error

        status = status or {}
        if status.get("status") == "error":
            return False, status.get("message") or "No se pudo preparar la pestaña de ChatGPT."

        return True, status.get("message")

    def sync_ref_title(self, title, metadata=None, timeout=6.0):
        payload = {
            "action": "SYNC_TEMPLATE_VARIABLES",
            "variables": {
                "REF_TITLE": title,
            },
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

        if metadata:
            payload["metadata"] = metadata

        request_id = self._new_request_id("template-sync")
        payload["request_id"] = request_id

        self._latest_payload = payload
        self._latest_template_ack = None
        self._template_sync_event.clear()
        self._register_pending_request(self._pending_template_requests, request_id)

        if not self._running or not self._loop:
            return False, self._last_error or "WebSocket bridge is not running."

        if not self.is_template_connected():
            return False, "La extension no esta conectada al bridge de variables."

        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(self._template_clients, payload),
            self._loop,
        )
        try:
            future.result(timeout=2)
        except Exception as exc:
            self._pending_template_requests.pop(request_id, None)
            self._last_error = str(exc)
            return False, self._last_error

        ok, error, ack = self._wait_pending_request(
            self._pending_template_requests,
            request_id,
            timeout,
            "La extension no confirmo la sincronizacion de REF_TITLE.",
        )
        if not ok:
            return False, error

        self._latest_template_ack = ack

        return True, None

    def run_journey(self, journey_id, tab_url_patterns=None):
        if not self._running or not self._loop:
            return False, self._last_error or "El servidor WebSocket no esta corriendo."

        if not self.is_control_connected():
            return False, "La extension no esta conectada al canal principal."

        request_id = self._new_request_id("run-journey")
        execution_id = self._new_execution_id()

        payload = {
            "action": "RUN_JOURNEY",
            "journey_id": journey_id,
            "request_id": request_id,
            "execution_id": execution_id,
        }

        if tab_url_patterns:
            payload["tab_url_patterns"] = tab_url_patterns

        with self._lock:
            self._journey_status[execution_id] = {
                "status": "queued",
                "message": "Journey encolado para ejecucion.",
                "journey_id": journey_id,
                "execution_id": execution_id,
            }
            self._latest_execution_id_by_journey[journey_id] = execution_id

        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(self._control_clients, payload),
            self._loop,
        )
        try:
            future.result(timeout=2)
        except Exception as exc:
            self._last_error = str(exc)
            return False, self._last_error, None

        return True, None, execution_id

    def validate_journey_execution(self, journey_id, timeout=12.0, tab_url_patterns=None):
        if not self._running or not self._loop:
            return False, self._last_error or "El servidor WebSocket no esta corriendo.", None

        if not self.is_control_connected():
            return False, "La extension no esta conectada al canal principal.", None

        self._execution_validation_event.clear()
        self._latest_execution_validation = None
        request_id = self._new_request_id("validate-journey")
        self._register_pending_request(self._pending_control_requests, request_id)

        payload = {
            "action": "VALIDATE_JOURNEY",
            "journey_id": journey_id,
            "request_id": request_id,
        }

        if tab_url_patterns:
            payload["tab_url_patterns"] = tab_url_patterns

        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(self._control_clients, payload),
            self._loop,
        )
        try:
            future.result(timeout=2)
        except Exception as exc:
            self._pending_control_requests.pop(request_id, None)
            self._last_error = str(exc)
            return False, self._last_error, None

        ok, error, validation = self._wait_pending_request(
            self._pending_control_requests,
            request_id,
            timeout,
            "La extension no devolvio el resultado de validacion a tiempo.",
        )
        if not ok:
            return False, error, None

        validation = validation or {}
        if validation.get("status") == "error":
            return False, validation.get("message") or "La validacion del journey fallo.", validation

        return True, None, validation

    def wait_for_journey_completion(
        self,
        execution_id,
        timeout=90.0,
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        completion_event = threading.Event()
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        seen_messages = set()

        with self._lock:
            if execution_id not in self._journey_status:
                self._journey_status[execution_id] = {
                    "status": "queued",
                    "message": "Esperando inicio...",
                    "execution_id": execution_id,
                }
            self._journey_completion_events[execution_id] = completion_event

        try:
            while datetime.now(timezone.utc).timestamp() < deadline:
                remaining = deadline - datetime.now(timezone.utc).timestamp()
                if remaining <= 0:
                    break

                completion_event.wait(timeout=min(0.5, remaining))

                status = self.get_journey_status(execution_id)
                if status:
                    message = status.get("message")
                    if message and message not in seen_messages and progress_callback:
                        progress_callback(message)
                        seen_messages.add(message)

                    current_status = status.get("status")
                    if current_status == "completed":
                        return True, None
                    if current_status == "error":
                        return False, message or "El journey fallo durante la ejecucion."

                completion_event.clear()
        finally:
            with self._lock:
                self._journey_completion_events.pop(execution_id, None)

        return False, "Timeout esperando la finalizacion del journey."

    def _run_server(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._loop.create_task(self._start_server())

        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._loop = None
            self._running = False

    async def _start_server(self):
        try:
            self._control_server = await websockets.serve(
                self._handle_control_client,
                self.host,
                self.control_port,
            )
            self._template_server = await websockets.serve(
                self._handle_template_client,
                self.host,
                self.template_port,
            )
            self._running = True
            self._last_error = None
            self._start_heartbeat()
        except OSError as exc:
            self._running = False
            self._last_error = str(exc)
            self._loop.call_soon(self._loop.stop)
        finally:
            self._ready_event.set()

    async def _handle_control_client(self, websocket):
        self._control_clients.add(websocket)
        self._control_connected_event.set()

        try:
            async for message in websocket:
                await self._handle_control_message(message)
        except Exception:
            pass
        finally:
            self._control_clients.discard(websocket)
            if not self._control_clients:
                self._control_connected_event.clear()

    async def _handle_template_client(self, websocket):
        self._template_clients.add(websocket)
        self._template_connected_event.set()

        try:
            if self._latest_payload:
                await websocket.send(json.dumps(self._latest_payload))

            async for message in websocket:
                await self._handle_template_message(message)
        except Exception:
            pass
        finally:
            self._template_clients.discard(websocket)
            if not self._template_clients:
                self._template_connected_event.clear()

    async def _handle_control_message(self, raw_message):
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        action = payload.get("action")
        if action == "JOURNEYS_LIST":
            journeys = payload.get("data") or []
            with self._lock:
                self._journeys = journeys
            self._journeys_updated_event.set()
            self._resolve_pending_request(
                self._pending_control_requests,
                payload.get("request_id"),
                payload,
            )
            return

        if action == "JOURNEY_STATUS":
            journey_id = payload.get("journey_id")
            execution_id = payload.get("execution_id") or journey_id
            if not execution_id:
                return

            with self._lock:
                self._journey_status[execution_id] = {
                    "status": payload.get("status"),
                    "message": payload.get("message"),
                    "journey_id": journey_id,
                    "execution_id": execution_id,
                }
                if journey_id:
                    self._latest_execution_id_by_journey[journey_id] = execution_id
                completion_event = self._journey_completion_events.get(execution_id)

            if completion_event:
                completion_event.set()
            return

        if action == "CHATGPT_TAB_STATUS":
            with self._lock:
                self._latest_chatgpt_tab_status = payload
            self._chatgpt_tab_ready_event.set()
            self._resolve_pending_request(
                self._pending_control_requests,
                payload.get("request_id"),
                payload,
            )
            return

        if action == "EXECUTION_VALIDATION_RESULT":
            with self._lock:
                self._latest_execution_validation = payload
            self._execution_validation_event.set()
            self._resolve_pending_request(
                self._pending_control_requests,
                payload.get("request_id"),
                payload,
            )

    async def _handle_template_message(self, raw_message):
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        if payload.get("action") != "TEMPLATE_VARIABLES_SYNCED":
            return

        with self._lock:
            self._latest_template_ack = payload

        self._resolve_pending_request(
            self._pending_template_requests,
            payload.get("request_id"),
            payload,
        )

        ack_updated_at = payload.get("updatedAt")
        expected_updated_at = None
        if self._latest_payload:
            expected_updated_at = self._latest_payload.get("updatedAt")

        if expected_updated_at and ack_updated_at == expected_updated_at:
            self._template_sync_event.set()

    async def _broadcast(self, clients, payload):
        message = json.dumps(payload)
        stale_clients = []

        for client in list(clients):
            try:
                await client.send(message)
            except Exception:
                stale_clients.append(client)

        for client in stale_clients:
            clients.discard(client)

    async def _shutdown(self):
        for client in list(self._control_clients):
            try:
                await client.close()
            except Exception:
                pass
        self._control_clients.clear()

        for client in list(self._template_clients):
            try:
                await client.close()
            except Exception:
                pass
        self._template_clients.clear()

        self._control_connected_event.clear()
        self._template_connected_event.clear()

        if self._control_server:
            self._control_server.close()
            await self._control_server.wait_closed()
            self._control_server = None

        if self._template_server:
            self._template_server.close()
            await self._template_server.wait_closed()
            self._template_server = None

        self._running = False
        self._loop.stop()