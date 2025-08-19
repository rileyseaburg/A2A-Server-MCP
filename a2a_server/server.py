"""
A2A Protocol Server Implementation

Main server implementation providing JSON-RPC 2.0 over HTTP(S) with support for
streaming, task management, and agent discovery.
"""

import asyncio
import json
import logging
from typing import Dict, Any, Optional, Callable, List
from datetime import datetime, timedelta
import uuid

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

from .models import (
    JSONRPCRequest, JSONRPCResponse, JSONRPCError,
    SendMessageRequest, SendMessageResponse,
    GetTaskRequest, GetTaskResponse,
    CancelTaskRequest, CancelTaskResponse,
    StreamMessageRequest, TaskStatusUpdateEvent,
    Task, TaskStatus, Message, Part,
    LiveKitTokenRequest, LiveKitTokenResponse
)
from .task_manager import TaskManager, InMemoryTaskManager
from .message_broker import MessageBroker, InMemoryMessageBroker
from .agent_card import AgentCard
from .livekit_bridge import create_livekit_bridge, LiveKitBridge


logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


class A2AServer:
    """Main A2A Protocol Server implementation."""
    
    def __init__(
        self,
        agent_card: AgentCard,
        task_manager: Optional[TaskManager] = None,
        message_broker: Optional[MessageBroker] = None,
        auth_callback: Optional[Callable[[str], bool]] = None
    ):
        self.agent_card = agent_card
        self.task_manager = task_manager or InMemoryTaskManager()
        self.message_broker = message_broker or InMemoryMessageBroker()
        self.auth_callback = auth_callback
        
        # Initialize LiveKit bridge if available
        try:
            self.livekit_bridge = create_livekit_bridge()
            if self.livekit_bridge:
                logger.info("LiveKit bridge initialized successfully")
            else:
                logger.info("LiveKit bridge not configured - media features disabled")
        except Exception as e:
            logger.warning(f"Failed to initialize LiveKit bridge: {e}")
            self.livekit_bridge = None
        
        # Create FastAPI app
        self.app = FastAPI(
            title=f"A2A Server - {agent_card.card.name}",
            description=agent_card.card.description,
            version=agent_card.card.version
        )
        
        # Add CORS middleware
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Method handlers
        self._method_handlers: Dict[str, Callable] = {
            "message/send": self._handle_send_message,
            "message/stream": self._handle_stream_message,
            "tasks/get": self._handle_get_task,
            "tasks/cancel": self._handle_cancel_task,
            "tasks/resubscribe": self._handle_resubscribe_task,
        }
        
        # Active streaming connections
        self._streaming_connections: Dict[str, List[asyncio.Queue]] = {}
        
        self._setup_routes()
    
    def _setup_routes(self) -> None:
        """Setup FastAPI routes."""
        
        @self.app.get("/.well-known/agent-card.json")
        async def get_agent_card():
            """Serve the agent card for discovery."""
            return JSONResponse(content=self.agent_card.to_dict())
        
        @self.app.post("/")
        async def handle_jsonrpc(
            request: Request,
            credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
        ):
            """Handle JSON-RPC 2.0 requests."""
            return await self._handle_jsonrpc_request(request, credentials)
        
        @self.app.get("/health")
        async def health_check():
            """Health check endpoint."""
            return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
        
        @self.app.get("/agents")
        async def discover_agents():
            """Discover other agents through the message broker."""
            agents = await self.message_broker.discover_agents()
            return [agent.model_dump() for agent in agents]
        
        @self.app.post("/v1/livekit/token", response_model=LiveKitTokenResponse)
        async def get_livekit_token(
            token_request: LiveKitTokenRequest,
            credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
        ):
            """Get a LiveKit access token for media sessions."""
            return await self._handle_livekit_token_request(token_request, credentials)
    
    async def _handle_jsonrpc_request(
        self,
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials]
    ) -> Response:
        """Handle incoming JSON-RPC request."""
        try:
            # Parse request body
            body = await request.body()
            try:
                request_data = json.loads(body)
            except json.JSONDecodeError:
                return self._create_error_response(None, -32700, "Parse error")
            
            # Validate JSON-RPC structure
            try:
                rpc_request = JSONRPCRequest.model_validate(request_data)
            except Exception:
                return self._create_error_response(
                    request_data.get("id"), -32600, "Invalid Request"
                )
            
            # Check authentication if required
            if self.agent_card.card.authentication and self.auth_callback:
                if not credentials or not self.auth_callback(credentials.credentials):
                    return self._create_error_response(
                        rpc_request.id, -32001, "Authentication failed"
                    )
            
            # Handle method
            method_handler = self._method_handlers.get(rpc_request.method)
            if not method_handler:
                return self._create_error_response(
                    rpc_request.id, -32601, "Method not found"
                )
            
            try:
                result = await method_handler(rpc_request.params or {})
                
                # Check if this is a streaming response
                if isinstance(result, StreamingResponse):
                    return result
                
                return self._create_success_response(rpc_request.id, result)
            
            except Exception as e:
                logger.error(f"Error handling method {rpc_request.method}: {e}")
                return self._create_error_response(
                    rpc_request.id, -32603, f"Internal error: {str(e)}"
                )
        
        except Exception as e:
            logger.error(f"Error processing JSON-RPC request: {e}")
            return self._create_error_response(None, -32603, "Internal error")
    
    def _create_success_response(self, request_id: Any, result: Any) -> JSONResponse:
        """Create a successful JSON-RPC response."""
        response = JSONRPCResponse(
            id=request_id,
            result=result
        )
        return JSONResponse(content=response.model_dump(exclude_none=True))
    
    def _create_error_response(self, request_id: Any, code: int, message: str) -> JSONResponse:
        """Create an error JSON-RPC response."""
        error = JSONRPCError(code=code, message=message)
        response = JSONRPCResponse(
            id=request_id,
            error=error.model_dump()
        )
        return JSONResponse(
            content=response.model_dump(exclude_none=True),
            status_code=400 if code != -32603 else 500
        )
    
    async def _handle_send_message(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle message/send method."""
        try:
            request = SendMessageRequest.model_validate(params)
        except Exception as e:
            raise ValueError(f"Invalid parameters: {e}")
        
        # Create or get task
        if request.task_id:
            task = await self.task_manager.get_task(request.task_id)
            if not task:
                raise ValueError(f"Task not found: {request.task_id}")
        else:
            task = await self.task_manager.create_task(
                title="Message processing",
                description="Processing incoming message"
            )
        
        # Update task status
        await self.task_manager.update_task_status(
            task.id, TaskStatus.WORKING, request.message
        )
        
        # Process the message (this would be implemented by specific agents)
        response_message = await self._process_message(request.message, request.skill_id)
        
        # Update task as completed
        await self.task_manager.update_task_status(
            task.id, TaskStatus.COMPLETED, response_message, final=True
        )
        
        # Publish message event
        await self.message_broker.publish_message(
            "external", self.agent_card.card.name, request.message
        )
        
        response = SendMessageResponse(
            task=task,
            message=response_message
        )
        return response.model_dump(mode='json')
    
    async def _handle_stream_message(self, params: Dict[str, Any]) -> StreamingResponse:
        """Handle message/stream method."""
        try:
            request = StreamMessageRequest.model_validate(params)
        except Exception as e:
            raise ValueError(f"Invalid parameters: {e}")
        
        # Check if streaming is supported
        if not (self.agent_card.card.capabilities and 
                self.agent_card.card.capabilities.streaming):
            raise ValueError("Streaming not supported")
        
        # Create task
        task = await self.task_manager.create_task(
            title="Streaming message processing",
            description="Processing streaming message"
        )
        
        # Create event queue for this connection
        event_queue = asyncio.Queue()
        task_id = task.id
        if task_id not in self._streaming_connections:
            self._streaming_connections[task_id] = []
        self._streaming_connections[task_id].append(event_queue)
        
        # Register task update handler
        async def task_update_handler(event: TaskStatusUpdateEvent):
            await event_queue.put(event)
        
        await self.task_manager.register_update_handler(task_id, task_update_handler)
        
        # Start processing in background
        asyncio.create_task(self._process_streaming_message(request, task))
        
        # Return streaming response
        async def generate_events():
            try:
                while True:
                    try:
                        # Wait for next event with timeout
                        event = await asyncio.wait_for(event_queue.get(), timeout=30.0)
                        
                        # Format as Server-Sent Event
                        event_data = {
                            "jsonrpc": "2.0",
                            "id": task_id,
                            "result": {"event": event.model_dump(mode='json')}
                        }
                        
                        yield f"data: {json.dumps(event_data)}\n\n"
                        
                        # Break if this is the final event
                        if event.final:
                            break
                    
                    except asyncio.TimeoutError:
                        # Send keepalive
                        yield "data: {}\n\n"
            
            finally:
                # Cleanup
                if task_id in self._streaming_connections:
                    try:
                        self._streaming_connections[task_id].remove(event_queue)
                        if not self._streaming_connections[task_id]:
                            del self._streaming_connections[task_id]
                    except ValueError:
                        pass
                
                await self.task_manager.unregister_update_handler(task_id, task_update_handler)
        
        return StreamingResponse(
            generate_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
    
    async def _handle_get_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tasks/get method."""
        try:
            request = GetTaskRequest.model_validate(params)
        except Exception as e:
            raise ValueError(f"Invalid parameters: {e}")
        
        task = await self.task_manager.get_task(request.task_id)
        if not task:
            raise ValueError(f"Task not found: {request.task_id}")
        
        response = GetTaskResponse(task=task)
        return response.model_dump(mode='json')
    
    async def _handle_cancel_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tasks/cancel method."""
        try:
            request = CancelTaskRequest.model_validate(params)
        except Exception as e:
            raise ValueError(f"Invalid parameters: {e}")
        
        task = await self.task_manager.cancel_task(request.task_id)
        if not task:
            raise ValueError(f"Task not found: {request.task_id}")
        
        response = CancelTaskResponse(task=task)
        return response.model_dump(mode='json')
    
    async def _handle_resubscribe_task(self, params: Dict[str, Any]) -> StreamingResponse:
        """Handle tasks/resubscribe method."""
        task_id = params.get("task_id")
        if not task_id:
            raise ValueError("task_id is required")
        
        task = await self.task_manager.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        
        # This is a simplified implementation - in a real system you'd
        # need to handle reconnection to existing streams
        return await self._handle_stream_message(params)
    
    async def _process_message(self, message: Message, skill_id: Optional[str] = None) -> Message:
        """Process an incoming message. Override this in subclasses."""
        # Default implementation - echo the message back
        response_parts = []
        for part in message.parts:
            if part.type == "text":
                response_parts.append(Part(
                    type="text",
                    content=f"Received: {part.content}"
                ))
            else:
                response_parts.append(part)
        
        return Message(parts=response_parts)
    
    async def _process_streaming_message(self, request: StreamMessageRequest, task: Task) -> None:
        """Process a streaming message with periodic updates."""
        try:
            # Update task status to working
            await self.task_manager.update_task_status(
                task.id, TaskStatus.WORKING, request.message
            )
            
            # Simulate processing with progress updates
            for i in range(5):
                await asyncio.sleep(1)  # Simulate work
                progress = (i + 1) / 5
                await self.task_manager.update_task_status(
                    task.id, 
                    TaskStatus.WORKING,
                    progress=progress
                )
            
            # Generate final response
            response_message = await self._process_message(request.message, request.skill_id)
            
            # Complete the task
            await self.task_manager.update_task_status(
                task.id, TaskStatus.COMPLETED, response_message, final=True
            )
        
        except Exception as e:
            logger.error(f"Error processing streaming message: {e}")
            await self.task_manager.update_task_status(
                task.id, TaskStatus.FAILED, final=True
            )
    
    def _validate_auth(self, credentials: Optional[HTTPAuthorizationCredentials]) -> bool:
        """Validate authentication credentials."""
        if self.agent_card.card.authentication and self.auth_callback:
            if not credentials or not self.auth_callback(credentials.credentials):
                return False
        return True
    
    async def _handle_livekit_token_request(
        self,
        token_request: LiveKitTokenRequest,
        credentials: Optional[HTTPAuthorizationCredentials]
    ) -> LiveKitTokenResponse:
        """Handle LiveKit token request with A2A authentication."""
        # Validate authentication
        if not self._validate_auth(credentials):
            raise HTTPException(status_code=401, detail="Authentication required")
        
        # Check if LiveKit bridge is available
        if not self.livekit_bridge:
            raise HTTPException(
                status_code=503,
                detail="LiveKit functionality not available - bridge not configured"
            )
        
        try:
            # Mint access token using LiveKit bridge
            access_token = self.livekit_bridge.mint_access_token(
                identity=token_request.identity,
                room_name=token_request.room_name,
                a2a_role=token_request.role,
                metadata=token_request.metadata,
                ttl_minutes=token_request.ttl_minutes
            )
            
            # Generate join URL
            join_url = self.livekit_bridge.generate_join_url(
                token_request.room_name,
                access_token
            )
            
            # Calculate expiration time
            expires_at = datetime.now() + timedelta(minutes=token_request.ttl_minutes)
            
            logger.info(f"Minted LiveKit token for {token_request.identity} in room {token_request.room_name}")
            
            return LiveKitTokenResponse(
                access_token=access_token,
                join_url=join_url,
                expires_at=expires_at
            )
            
        except Exception as e:
            logger.error(f"Failed to mint LiveKit token: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to generate token: {str(e)}")
    
    async def start(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        """Start the A2A server."""
        # Start message broker
        await self.message_broker.start()
        
        # Register this agent
        await self.message_broker.register_agent(self.agent_card.card)
        
        logger.info(f"Starting A2A server for {self.agent_card.card.name}")
        logger.info(f"Agent card available at: http://{host}:{port}/.well-known/agent-card.json")
        
        # Start the server
        config = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()
    
    async def stop(self) -> None:
        """Stop the A2A server."""
        # Unregister this agent
        await self.message_broker.unregister_agent(self.agent_card.card.name)
        
        # Stop message broker
        await self.message_broker.stop()
        
        logger.info(f"Stopped A2A server for {self.agent_card.card.name}")


# Custom agent implementations would inherit from this
class CustomA2AAgent(A2AServer):
    """Base class for custom A2A agent implementations."""
    
    async def _process_message(self, message: Message, skill_id: Optional[str] = None) -> Message:
        """Override this method to implement custom message processing logic."""
        raise NotImplementedError("Subclasses must implement _process_message")