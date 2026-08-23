"""
FastAPI Web Server for MokioClaw CodeAgent

Provides:
- REST API for session management, task submission, and status queries
- WebSocket endpoint for real-time event streaming from agent execution
- Static file serving for the React frontend (web/dist)
- Health check and system info endpoints
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import logging

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger("mokioclaw.web")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "web"
DIST_DIR = WEB_ROOT / "dist"
STATIC_DIR = WEB_ROOT / "static"  # fallback for dev mode

# ---------------------------------------------------------------------------
# In-memory state (can be replaced with DB/Redis later)
# ---------------------------------------------------------------------------

class SessionState(BaseModel):
    """Tracks an active agent session."""
    session_id: str
    task: str
    status: str = "pending"  # pending | running | completed | failed
    events: list[dict[str, Any]] = []
    final_answer: str = ""
    started_at: str = ""
    finished_at: str = ""


# Global registries
_sessions: dict[str, SessionState] = {}
_ws_clients: dict[str, set[WebSocket]] = {}  # session_id -> set of connected WS


# ---------------------------------------------------------------------------
# Pydantic models for API
# ---------------------------------------------------------------------------

class TaskRequest(BaseModel):
    """Submit a new task to the agent."""
    task: str = Field(..., min_length=1, max_length=4096, description="Task description")
    workspace: str | None = Field(None, description="Workspace directory path")
    max_attempts: int = Field(3, ge=1, le=10, description="Max planning attempts")
    approval_mode: str = Field("inline", pattern="^(inline|auto|deny)$")
    agent_mode: str = Field("auto", pattern="^(auto|plan|approve|edit)$")
    checkpoint_mode: str = Field("light", pattern="^(light|strict|off)$")
    trace_mode: str = Field("on", pattern="^(on|off)$")
    safe_mode: bool = Field(False)


class TaskResponse(BaseModel):
    session_id: str
    status: str
    task: str


class SessionInfo(BaseModel):
    session_id: str
    task: str
    status: str
    final_answer: str = ""
    event_count: int = 0
    started_at: str = ""
    finished_at: str = ""


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"
    sessions_active: int = 0
    sessions_total: int = 0


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle manager."""
    # Startup
    app.state.start_time = _utc_now_iso()
    logger.info("MokioClaw Web Server starting up")
    yield
    # Shutdown
    logger.info("MokioClaw Web Server shutting down")
    # Close all active WebSocket connections
    for clients in _ws_clients.values():
        for ws in list(clients):
            try:
                await ws.close()
            except Exception:
                pass
    _ws_clients.clear()
    _sessions.clear()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MokioClaw CodeAgent API",
    description="REST + WebSocket API for the MokioClaw AI coding agent.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware — allow the Vite dev server and any frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_session(session_id: str) -> SessionState:
    if session_id not in _sessions:
        _sessions[session_id] = SessionState(session_id=session_id, task="")
    return _sessions[session_id]


def _emit_event(session_id: str, event: dict[str, Any]) -> None:
    """Broadcast an event to all WebSocket clients subscribed to a session."""
    clients = _ws_clients.get(session_id, set())
    payload = json.dumps(event, ensure_ascii=False)
    for ws in list(clients):
        try:
            asyncio.create_task(ws.send_text(payload))
        except Exception:
            clients.discard(ws)


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """System health check."""
    return HealthResponse(
        status="ok",
        sessions_active=sum(1 for s in _sessions.values() if s.status == "running"),
        sessions_total=len(_sessions),
    )


@app.get("/info")
async def server_info() -> dict[str, Any]:
    """Detailed server information."""
    return {
        "name": "MokioClaw CodeAgent",
        "version": "0.1.0",
        "started_at": app.state.start_time,
        "endpoints": {
            "rest_api": "/docs",
            "websocket": "/ws/{session_id}",
            "frontend": "/",
        },
        "features": [
            "Real-time event streaming via WebSocket",
            "Session management (create, list, get, delete)",
            "Task submission with configurable parameters",
            "CORS enabled for frontend development",
        ],
    }


# --- Sessions ---------------------------------------------------------------

@app.post("/api/tasks", response_model=TaskResponse, status_code=201)
async def submit_task(req: TaskRequest) -> TaskResponse:
    """
    Submit a new task to the agent.

    Returns a session_id that can be used to:
    - Subscribe to real-time events via WebSocket at `/ws/{session_id}`
    - Query session status via GET `/api/sessions/{session_id}`
    """
    session_id = f"session-{uuid.uuid4().hex[:12]}"
    session = _get_or_create_session(session_id)
    session.task = req.task
    session.status = "pending"
    session.started_at = _utc_now_iso()
    session.events = []
    session.final_answer = ""

    logger.info("New task submitted: %s (session=%s)", req.task[:80], session_id)

    # TODO: Kick off background agent execution here
    # For now we simulate a quick completion; replace with actual stream_agent_events call
    asyncio.create_task(_run_agent_background(session_id, req))

    return TaskResponse(session_id=session_id, status="pending", task=req.task)


@app.get("/api/sessions", response_model=list[SessionInfo])
async def list_sessions() -> list[SessionInfo]:
    """List all sessions."""
    result = []
    for sid, s in sorted(_sessions.items(), key=lambda x: x[1].started_at, reverse=True):
        result.append(SessionInfo(
            session_id=s.session_id,
            task=s.task,
            status=s.status,
            final_answer=s.final_answer,
            event_count=len(s.events),
            started_at=s.started_at,
            finished_at=s.finished_at,
        ))
    return result


@app.get("/api/sessions/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str) -> SessionInfo:
    """Get details of a specific session."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return SessionInfo(
        session_id=session.session_id,
        task=session.task,
        status=session.status,
        final_answer=session.final_answer,
        event_count=len(session.events),
        started_at=session.started_at,
        finished_at=session.finished_at,
    )


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """Delete a session and disconnect its WebSocket clients."""
    session = _sessions.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    # Disconnect all WebSocket clients
    clients = _ws_clients.pop(session_id, set())
    for ws in clients:
        try:
            await ws.close()
        except Exception:
            pass
    logger.info("Session deleted: %s", session_id)
    return {"detail": f"Session {session_id} deleted"}


# --- Agent Execution Background Task ----------------------------------------

async def _run_agent_background(session_id: str, req: TaskRequest) -> None:
    """
    Background coroutine that runs the agent for a given session.

    This is where you would integrate with the existing
    `stream_agent_events()` from mokioclaw.orchestration.agent.

    For now, it simulates a simple workflow:
    1. Emit a 'task_started' event
    2. Simulate some processing time
    3. Emit a 'task_completed' event with a placeholder answer
    """
    session = _get_or_create_session(session_id)
    session.status = "running"

    # Event 1: task started
    evt = {
        "type": "custom_event",
        "event": {
            "type": "task_started",
            "session_id": session_id,
            "task": req.task,
        },
    }
    session.events.append(evt)
    _emit_event(session_id, evt)

    # TODO: Replace this simulation with actual agent execution:
    #
    #   from mokioclaw.orchestration.agent import stream_agent_events
    #   for event in stream_agent_events(
    #       req.task,
    #       workspace=Path(req.workspace) if req.workspace else None,
    #       max_attempts=req.max_attempts,
    #       approval_mode=req.approval_mode,
    #       agent_mode=req.agent_mode,
    #       checkpoint_mode=req.checkpoint_mode,
    #       trace_mode=req.trace_mode,
    #       safe_mode=req.safe_mode,
    #   ):
    #       session.events.append(event)
    #       _emit_event(session_id, event)
    #       if event.get("type") == "graph_event":
    #           ans = _extract_final_answer(event)
    #           if ans:
    #               session.final_answer = ans
    #
    #   session.status = "completed"
    #   session.finished_at = _utc_now_iso()

    # Simulated delay
    await asyncio.sleep(2)

    # Simulated events
    for i in range(3):
        evt = {
            "type": "log",
            "level": "info",
            "message": f"Processing step {i+1}/3...",
        }
        session.events.append(evt)
        _emit_event(session_id, evt)
        await asyncio.sleep(1)

    # Completion
    session.final_answer = f"Task completed: {req.task}"
    session.status = "completed"
    session.finished_at = _utc_now_iso()

    evt = {
        "type": "custom_event",
        "event": {
            "type": "task_completed",
            "session_id": session_id,
            "final_answer": session.final_answer,
        },
    }
    session.events.append(evt)
    _emit_event(session_id, evt)

    logger.info("Task completed: %s (session=%s)", req.task[:80], session_id)


def _extract_final_answer(event: dict[str, Any]) -> str:
    """Extract final answer from a graph_event (mirrors CLI logic)."""
    payload = event.get("event")
    if not isinstance(payload, dict):
        return ""
    for node in ("final", "chat_responder"):
        update = payload.get(node)
        if isinstance(update, dict):
            answer = str(update.get("final_answer") or update.get("chat_response") or "")
            if answer:
                return answer
    return ""


# ---------------------------------------------------------------------------
# WebSocket Endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """
    WebSocket endpoint for real-time event streaming.

    Connect to `/ws/{session_id}` to receive live events from an agent session.
    Events are sent as JSON strings.

    Example client (JavaScript):
        const ws = new WebSocket('ws://localhost:8000/ws/session-abc123');
        ws.onmessage = (evt) => console.log(JSON.parse(evt.data));
    """
    # Check if session exists
    if session_id not in _sessions:
        await websocket.close(code=4004, reason="Session not found")
        return

    await websocket.accept()
    session = _get_or_create_session(session_id)

    # Register this client
    if session_id not in _ws_clients:
        _ws_clients[session_id] = set()
    _ws_clients[session_id].add(websocket)

    logger.info("WebSocket client connected: session=%s", session_id)

    try:
        while True:
            # Wait for messages from client (e.g., keep-alive, commands)
            data = await websocket.receive_text()
            msg = json.loads(data) if data.strip() else {}
            msg_type = msg.get("type", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "subscribe":
                # Client confirms interest in this session
                await websocket.send_json({
                    "type": "subscribed",
                    "session_id": session_id,
                    "status": session.status,
                    "event_count": len(session.events),
                })
            elif msg_type == "get_history":
                # Send all past events
                await websocket.send_json({
                    "type": "history",
                    "events": session.events[-100:],  # last 100 events
                })
            else:
                # Echo unknown messages back
                await websocket.send_json({"type": "ack", "data": msg})
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected: session=%s", session_id)
    except Exception as exc:
        logger.error("WebSocket error: session=%s, error=%s", session_id, exc)
    finally:
        # Unregister client
        clients = _ws_clients.get(session_id, set())
        clients.discard(websocket)
        if not clients:
            _ws_clients.pop(session_id, None)


# ---------------------------------------------------------------------------
# Frontend Serving
# ---------------------------------------------------------------------------

def _serve_frontend(path: str) -> FileResponse:
    """Serve the React frontend SPA."""
    # Try built dist first
    if DIST_DIR.exists():
        file_path = DIST_DIR / (path.lstrip("/") or "index.html")
        if file_path.is_file():
            return FileResponse(file_path)
    # Fallback: serve from web root (dev mode)
    if STATIC_DIR.exists():
        file_path = STATIC_DIR / (path.lstrip("/") or "index.html")
        if file_path.is_file():
            return FileResponse(file_path)
    # Default to index.html for SPA routing
    default = DIST_DIR / "index.html" if DIST_DIR.exists() else STATIC_DIR / "index.html"
    if default.is_file():
        return FileResponse(default)
    raise HTTPException(status_code=404, detail="Frontend not found. Run `npm run build` in the web/ directory.")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str) -> FileResponse:
    """Catch-all route for SPA frontend."""
    return _serve_frontend(full_path)


# ---------------------------------------------------------------------------
# Entry point helper
# ---------------------------------------------------------------------------

def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the web server using uvicorn."""
    import uvicorn
    uvicorn.run(
        "mokioclaw.web.server:app",
        host=host,
        port=port,
        reload=True,
    )


if __name__ == "__main__":
    run_server()
