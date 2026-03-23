import asyncio
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets

import logger as log


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
        log.info("wait_for_connections_start", timeout=timeout)
        if not self._control_connected_event.wait(timeout):
            log.error("wait_for_connections_control_timeout", timeout=timeout)
            return False, "La extension web no se conecto al canal principal (8765)."

        log.info("wait_for_connections_control_ok")

        if not self._template_connected_event.wait(timeout):
            log.error("wait_for_connections_template_timeout", timeout=timeout)
            return False, "La extension web no se conecto al bridge de variables (8766)."

        log.info("wait_for_connections_all_ok")
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
        log.info(
            "request_journeys_called",
            running=self._running,
            control_connected=self.is_control_connected(),
        )
        if not self._running or not self._loop:
            log.error("request_journeys_no_server", error=self._last_error)
            return False, self._last_error or "El servidor WebSocket no esta corriendo."

        if not self.is_control_connected():
            log.error("request_journeys_not_connected")
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
            log.info("request_journeys_dispatched", request_id=request_id)
        except Exception as exc:
            self._pending_control_requests.pop(request_id, None)
            self._last_error = str(exc)
            log.error(
                "request_journeys_dispatch_failed",
                request_id=request_id,
                error=str(exc),
            )
            return False, self._last_error

        return True, None

    def wait_for_journeys(self, timeout=4.0):
        request_id = self._latest_journeys_request_id
        log.info("wait_for_journeys_start", timeout=timeout, request_id=request_id)
        if request_id:
            ok, error, _payload = self._wait_pending_request(
                self._pending_control_requests,
                request_id,
                timeout,
                "La extension no devolvio la lista de journeys a tiempo.",
            )
            self._latest_journeys_request_id = None
            if ok:
                log.info("wait_for_journeys_ok", request_id=request_id)
                return True, None
            log.error("wait_for_journeys_failed", request_id=request_id, error=error)
            return False, error

        if self._journeys_updated_event.wait(timeout):
            log.info("wait_for_journeys_event_ok")
            return True, None
        log.error("wait_for_journeys_timeout", timeout=timeout)
        return False, "La extension no devolvio la lista de journeys a tiempo."

    def prepare_chatgpt_tab(self, timeout=15.0, tab_url_patterns=None):
        log.info(
            "prepare_chatgpt_tab_start",
            timeout=timeout,
            tab_url_patterns=tab_url_patterns,
        )
        if not self._running or not self._loop:
            log.error("prepare_chatgpt_tab_no_server", error=self._last_error)
            return False, self._last_error or "El servidor WebSocket no esta corriendo."

        if not self.is_control_connected():
            log.error("prepare_chatgpt_tab_not_connected")
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
            log.info("prepare_chatgpt_tab_dispatched", request_id=request_id)
        except Exception as exc:
            self._pending_control_requests.pop(request_id, None)
            self._last_error = str(exc)
            log.error(
                "prepare_chatgpt_tab_dispatch_failed",
                request_id=request_id,
                error=str(exc),
            )
            return False, self._last_error

        ok, error, status = self._wait_pending_request(
            self._pending_control_requests,
            request_id,
            timeout,
            "La extension no preparo una pestaña de ChatGPT a tiempo.",
        )
        if not ok:
            log.error("prepare_chatgpt_tab_failed", request_id=request_id, error=error)
            return False, error

        status = status or {}
        if status.get("status") == "error":
            error = status.get("message") or "No se pudo preparar la pestaña de ChatGPT."
            log.error(
                "prepare_chatgpt_tab_failed",
                request_id=request_id,
                error=error,
                status_payload=status,
            )
            return False, error

        log.info("prepare_chatgpt_tab_success", request_id=request_id, message=status.get("message"))
        return True, status.get("message")

    def sync_ref_title(self, title, metadata=None, timeout=6.0):
        log.info(
            "sync_ref_title_start",
            title=title,
            metadata=metadata,
            timeout=timeout,
        )
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
            log.error("sync_ref_title_no_server", error=self._last_error)
            return False, self._last_error or "WebSocket bridge is not running."

        if not self.is_template_connected():
            log.error("sync_ref_title_not_connected")
            return False, "La extension no esta conectada al bridge de variables."

        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(self._template_clients, payload),
            self._loop,
        )
        try:
            future.result(timeout=2)
            log.info("sync_ref_title_dispatched", request_id=request_id)
        except Exception as exc:
            self._pending_template_requests.pop(request_id, None)
            self._last_error = str(exc)
            log.error(
                "sync_ref_title_dispatch_failed",
                request_id=request_id,
                error=str(exc),
            )
            return False, self._last_error

        ok, error, ack = self._wait_pending_request(
            self._pending_template_requests,
            request_id,
            timeout,
            "La extension no confirmo la sincronizacion de REF_TITLE.",
        )
        if not ok:
            log.error("sync_ref_title_failed", request_id=request_id, error=error)
            return False, error

        self._latest_template_ack = ack

        log.info(
            "sync_ref_title_ok",
            request_id=request_id,
            ack_updated_at=ack.get("updatedAt") if ack else None,
            variable_names=ack.get("variableNames") if ack else None,
        )

        return True, None

    def run_journey(self, journey_id, tab_url_patterns=None):
        log.journey_event(
            "run_journey_called",
            journey_id=journey_id,
            tab_url_patterns=tab_url_patterns,
            control_connected=self.is_control_connected(),
        )
        if not self._running or not self._loop:
            log.error("run_journey_no_server", journey_id=journey_id)
            return False, self._last_error or "El servidor WebSocket no esta corriendo."

        if not self.is_control_connected():
            log.error("run_journey_not_connected", journey_id=journey_id)
            return False, "La extension no esta conectada al canal principal."

        request_id = self._new_request_id("run-journey")
        execution_id = self._new_execution_id()

        log.journey_event(
            "run_journey_ids_assigned",
            journey_id=journey_id,
            execution_id=execution_id,
            request_id=request_id,
        )

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
            log.journey_event(
                "run_journey_dispatched",
                journey_id=journey_id,
                execution_id=execution_id,
            )
        except Exception as exc:
            self._last_error = str(exc)
            log.error(
                "run_journey_dispatch_failed",
                journey_id=journey_id,
                execution_id=execution_id,
                error=str(exc),
            )
            return False, self._last_error, None

        return True, None, execution_id

    def validate_journey_execution(self, journey_id, timeout=12.0, tab_url_patterns=None):
        log.info(
            "validate_journey_execution_start",
            journey_id=journey_id,
            timeout=timeout,
            tab_url_patterns=tab_url_patterns,
        )
        if not self._running or not self._loop:
            log.error("validate_journey_execution_no_server", journey_id=journey_id)
            return False, self._last_error or "El servidor WebSocket no esta corriendo.", None

        if not self.is_control_connected():
            log.error("validate_journey_execution_not_connected", journey_id=journey_id)
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
            log.info(
                "validate_journey_execution_dispatched",
                journey_id=journey_id,
                request_id=request_id,
            )
        except Exception as exc:
            self._pending_control_requests.pop(request_id, None)
            self._last_error = str(exc)
            log.error(
                "validate_journey_execution_dispatch_failed",
                journey_id=journey_id,
                request_id=request_id,
                error=str(exc),
            )
            return False, self._last_error, None

        ok, error, validation = self._wait_pending_request(
            self._pending_control_requests,
            request_id,
            timeout,
            "La extension no devolvio el resultado de validacion a tiempo.",
        )
        if not ok:
            log.error(
                "validate_journey_execution_failed",
                journey_id=journey_id,
                request_id=request_id,
                error=error,
            )
            return False, error, None

        validation = validation or {}
        if validation.get("status") == "error":
            error = validation.get("message") or "La validacion del journey fallo."
            log.error(
                "validate_journey_execution_error_status",
                journey_id=journey_id,
                request_id=request_id,
                error=error,
                validation=validation,
            )
            return False, error, validation

        log.info(
            "validate_journey_execution_ok",
            journey_id=journey_id,
            request_id=request_id,
            validation=validation,
        )
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
        last_status = None

        log.journey_event(
            "wait_for_journey_start",
            execution_id=execution_id,
            timeout=timeout,
        )

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

                triggered = completion_event.wait(timeout=min(0.5, remaining))

                status = self.get_journey_status(execution_id)
                if status:
                    current_status = status.get("status")
                    message = status.get("message")

                    if current_status != last_status:
                        log.journey_event(
                            "journey_status_changed",
                            execution_id=execution_id,
                            old_status=last_status,
                            new_status=current_status,
                            message=message,
                            event_triggered=triggered,
                        )
                        last_status = current_status

                    if message and message not in seen_messages and progress_callback:
                        progress_callback(message)
                        seen_messages.add(message)

                    if current_status == "completed":
                        log.journey_event(
                            "journey_completed",
                            execution_id=execution_id,
                            total_messages=len(seen_messages),
                        )
                        return True, None
                    if current_status == "error":
                        log.journey_event(
                            "journey_failed",
                            execution_id=execution_id,
                            error_message=message,
                        )
                        return False, message or "El journey fallo durante la ejecucion."
                else:
                    log.warning(
                        "journey_status_not_found_in_dict",
                        execution_id=execution_id,
                        known_ids=list(self._journey_status.keys()),
                    )

                completion_event.clear()
        finally:
            with self._lock:
                self._journey_completion_events.pop(execution_id, None)

        elapsed = timeout - max(deadline - datetime.now(timezone.utc).timestamp(), 0)
        log.error(
            "journey_wait_timeout",
            execution_id=execution_id,
            timeout=timeout,
            elapsed=round(elapsed, 2),
            last_status=last_status,
            seen_message_count=len(seen_messages),
        )
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
        log.info(
            "ws_server_starting",
            control_port=self.control_port,
            template_port=self.template_port,
            host=self.host,
        )
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
            log.info(
                "ws_server_started",
                control_port=self.control_port,
                template_port=self.template_port,
            )
        except OSError as exc:
            self._running = False
            self._last_error = str(exc)
            log.error("ws_server_failed", error=str(exc))
            self._loop.call_soon(self._loop.stop)
        finally:
            self._ready_event.set()

    async def _handle_control_client(self, websocket):
        client_id = str(id(websocket))
        log.info(
            "control_client_connected",
            client_id=client_id,
            total_clients=len(self._control_clients) + 1,
        )
        self._control_clients.add(websocket)
        self._control_connected_event.set()

        try:
            async for message in websocket:
                await self._handle_control_message(message)
        except Exception as exc:
            log.warning("control_client_error", client_id=client_id, error=str(exc))
        finally:
            self._control_clients.discard(websocket)
            if not self._control_clients:
                self._control_connected_event.clear()
            log.info(
                "control_client_disconnected",
                client_id=client_id,
                remaining_clients=len(self._control_clients),
            )

    async def _handle_template_client(self, websocket):
        client_id = str(id(websocket))
        log.info(
            "template_client_connected",
            client_id=client_id,
            total_clients=len(self._template_clients) + 1,
        )
        self._template_clients.add(websocket)
        self._template_connected_event.set()

        try:
            if self._latest_payload:
                log.debug(
                    "template_replaying_latest_payload",
                    client_id=client_id,
                    action=self._latest_payload.get("action"),
                )
                await websocket.send(json.dumps(self._latest_payload))

            async for message in websocket:
                await self._handle_template_message(message)
        except Exception as exc:
            log.warning("template_client_error", client_id=client_id, error=str(exc))
        finally:
            self._template_clients.discard(websocket)
            if not self._template_clients:
                self._template_connected_event.clear()
            log.info(
                "template_client_disconnected",
                client_id=client_id,
                remaining_clients=len(self._template_clients),
            )

    async def _handle_control_message(self, raw_message):
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            log.error(
                "control_message_parse_error",
                error=str(exc),
                raw_preview=raw_message[:200],
            )
            return

        action = payload.get("action")
        log.ws_sent("received", action or "UNKNOWN", payload, port=self.control_port)
        if action == "JOURNEYS_LIST":
            journeys = payload.get("data") or []
            log.info(
                "journeys_list_received",
                count=len(journeys),
                request_id=payload.get("request_id"),
                journey_ids=[journey.get("id") for journey in journeys],
            )
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
            status = payload.get("status")
            message = payload.get("message")

            log.journey_event(
                "journey_status_received",
                execution_id=execution_id,
                journey_id=journey_id,
                status=status,
                message=message,
            )
            if not execution_id:
                log.error("journey_status_missing_execution_id", raw_payload=payload)
                return

            with self._lock:
                self._journey_status[execution_id] = {
                    "status": status,
                    "message": message,
                    "journey_id": journey_id,
                    "execution_id": execution_id,
                }
                if journey_id:
                    self._latest_execution_id_by_journey[journey_id] = execution_id
                completion_event = self._journey_completion_events.get(execution_id)

            if completion_event:
                log.debug(
                    "journey_completion_event_set",
                    execution_id=execution_id,
                    status=status,
                )
                completion_event.set()
            else:
                log.warning(
                    "journey_completion_event_not_found",
                    execution_id=execution_id,
                    known_execution_ids=list(self._journey_completion_events.keys()),
                )
            return

        if action == "CHATGPT_TAB_STATUS":
            log.info(
                "chatgpt_tab_status_received",
                status=payload.get("status"),
                tab_id=payload.get("tab_id"),
                url=payload.get("url"),
                message=payload.get("message"),
                request_id=payload.get("request_id"),
            )
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
            log.info(
                "execution_validation_result_received",
                journey_id=payload.get("journey_id"),
                status=payload.get("status"),
                message=payload.get("message"),
                missing_variables=payload.get("missing_variables"),
                missing_texts=payload.get("missing_texts"),
                page=payload.get("page"),
                request_id=payload.get("request_id"),
            )
            with self._lock:
                self._latest_execution_validation = payload
            self._execution_validation_event.set()
            self._resolve_pending_request(
                self._pending_control_requests,
                payload.get("request_id"),
                payload,
            )
            return

        if action == "HEARTBEAT_ACK":
            log.debug("heartbeat_ack_received", ts=payload.get("ts"))
            return

        log.warning(
            "control_message_unknown_action",
            action=action,
            payload_keys=list(payload.keys()),
        )

    async def _handle_template_message(self, raw_message):
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            log.error(
                "template_message_parse_error",
                error=str(exc),
                raw_preview=raw_message[:200],
            )
            return

        log.ws_sent("received", payload.get("action") or "UNKNOWN", payload, port=self.template_port)

        if payload.get("action") != "TEMPLATE_VARIABLES_SYNCED":
            log.warning(
                "template_message_unknown_action",
                action=payload.get("action"),
                payload_keys=list(payload.keys()),
            )
            return

        with self._lock:
            self._latest_template_ack = payload

        log.info(
            "template_variables_synced_received",
            request_id=payload.get("request_id"),
            updated_at=payload.get("updatedAt"),
            variable_names=payload.get("variableNames"),
        )

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
        action = payload.get("action", "UNKNOWN")
        port = self.control_port if clients is self._control_clients else self.template_port
        log.ws_sent("sent", action, payload, port=port)

        message = json.dumps(payload)
        stale_clients = []
        sent_count = 0

        for client in list(clients):
            try:
                await client.send(message)
                sent_count += 1
            except Exception as exc:
                log.warning("broadcast_client_error", action=action, error=str(exc))
                stale_clients.append(client)

        for client in stale_clients:
            clients.discard(client)

        if sent_count == 0:
            log.error(
                "broadcast_no_clients_reached",
                action=action,
                total_clients=len(clients),
                port=port,
            )
        else:
            log.debug("broadcast_sent", action=action, sent_to=sent_count, port=port)

    async def _shutdown(self):
        log.info("ws_shutdown_start")
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
        log.info("ws_shutdown_complete")
        self._loop.stop()