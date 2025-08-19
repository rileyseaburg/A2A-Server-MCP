#!/usr/bin/env python3
"""
A2A Server Runner

Main entry point for running A2A server instances.
"""

import asyncio
import logging
import sys
import argparse
from typing import Optional

from a2a_server.config import load_config, create_agent_config
from a2a_server.agent_card import create_agent_card
from a2a_server.task_manager import InMemoryTaskManager
from a2a_server.message_broker import MessageBroker, InMemoryMessageBroker
from a2a_server.server import A2AServer, CustomA2AAgent
from a2a_server.enhanced_server import EnhancedA2AServer, create_enhanced_agent_card
from a2a_server.models import Message, Part


class SimpleEchoAgent(CustomA2AAgent):
    """Simple echo agent implementation."""
    
    async def _process_message(self, message: Message, skill_id: Optional[str] = None) -> Message:
        """Echo messages back with a prefix."""
        response_parts = []
        for part in message.parts:
            if part.type == "text":
                response_parts.append(Part(
                    type="text",
                    content=f"Echo: {part.content}"
                ))
            else:
                response_parts.append(part)
        
        return Message(parts=response_parts)


def setup_logging(level: str = "INFO"):
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


async def create_server(
    agent_name: str,
    agent_description: str,
    port: int,
    use_redis: bool = False,
    use_enhanced: bool = True
) -> A2AServer:
    """Create an A2A server instance."""
    # Create agent configuration
    agent_config = create_agent_config(
        name=agent_name,
        description=agent_description,
        port=port
    )
    
    # Create components
    task_manager = InMemoryTaskManager()
    
    if use_redis:
        config = load_config()
        message_broker = MessageBroker(config.redis_url)
    else:
        message_broker = InMemoryMessageBroker()
    
    # Create authentication callback if needed
    config = load_config()
    auth_callback = None
    if config.auth_enabled and config.auth_tokens:
        def auth_callback(token: str) -> bool:
            return token in config.auth_tokens.values()
    
    if use_enhanced:
        # Create enhanced agent card with MCP tool capabilities
        agent_card = create_enhanced_agent_card()
        agent_card.card.name = agent_config.name
        agent_card.card.description = agent_config.description
        agent_card.card.url = agent_config.base_url or f"http://localhost:{port}"
        agent_card.card.provider.organization = agent_config.organization
        agent_card.card.provider.url = agent_config.organization_url
        
        # Create and return enhanced server
        return EnhancedA2AServer(
            agent_card=agent_card,
            task_manager=task_manager,
            message_broker=message_broker,
            auth_callback=auth_callback
        )
    else:
        # Create basic agent card
        base_url = agent_config.base_url or f"http://localhost:{port}"
        agent_card = (create_agent_card(
            name=agent_config.name,
            description=agent_config.description,
            url=base_url,
            organization=agent_config.organization,
            organization_url=agent_config.organization_url
        )
        .with_streaming()
        .with_push_notifications()
        .with_skill(
            skill_id="echo",
            name="Echo Messages",
            description="Echoes back messages with a prefix",
            input_modes=["text"],
            output_modes=["text"],
            examples=[{
                "input": {"type": "text", "content": "Hello!"},
                "output": {"type": "text", "content": "Echo: Hello!"}
            }]
        )
        .build())
        
        # Create and return basic server
        return SimpleEchoAgent(
            agent_card=agent_card,
            task_manager=task_manager,
            message_broker=message_broker,
            auth_callback=auth_callback
        )


async def run_server(args):
    """Run a single server instance."""
    setup_logging(args.log_level)
    
    server = await create_server(
        agent_name=args.name,
        agent_description=args.description,
        port=args.port,
        use_redis=args.redis,
        use_enhanced=args.enhanced
    )
    
    print(f"Starting A2A server '{args.name}' on port {args.port}")
    print(f"Agent card: http://localhost:{args.port}/.well-known/agent-card.json")
    print("Press Ctrl+C to stop")
    
    try:
        await server.start(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nShutting down...")
        await server.stop()


async def run_multiple_servers():
    """Run multiple example servers concurrently."""
    setup_logging("INFO")
    
    servers = [
        await create_server("Enhanced Agent 1", "First enhanced agent with MCP tools", 8001, False, True),
        await create_server("Enhanced Agent 2", "Second enhanced agent with MCP tools", 8002, False, True),
        await create_server("Basic Echo Agent", "Basic echo agent", 8003, False, False),
    ]
    
    print("Starting multiple A2A servers:")
    for i, server in enumerate(servers, 1):
        port = 8000 + i
        print(f"  Agent {i}: http://localhost:{port}/.well-known/agent-card.json")
    
    print("Press Ctrl+C to stop all servers")
    
    try:
        # Start all servers concurrently
        tasks = []
        for i, server in enumerate(servers, 1):
            port = 8000 + i
            task = asyncio.create_task(server.start(host="0.0.0.0", port=port))
            tasks.append(task)
        
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\nShutting down all servers...")
        for server in servers:
            await server.stop()


def main():
    parser = argparse.ArgumentParser(description="A2A Server Runner")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Single server command
    single_parser = subparsers.add_parser("run", help="Run a single A2A server")
    single_parser.add_argument("--name", default="Enhanced A2A Agent", help="Agent name")
    single_parser.add_argument("--description", default="An A2A agent with MCP tool integration", help="Agent description")
    single_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    single_parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    single_parser.add_argument("--redis", action="store_true", help="Use Redis message broker")
    single_parser.add_argument("--enhanced", action="store_true", default=True, help="Use enhanced MCP-enabled agents")
    single_parser.add_argument("--basic", dest="enhanced", action="store_false", help="Use basic echo agent only")
    single_parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    
    # Multiple servers command
    multi_parser = subparsers.add_parser("multi", help="Run multiple example servers")
    
    # Example configurations
    examples_parser = subparsers.add_parser("examples", help="Show example configurations")
    
    args = parser.parse_args()
    
    if args.command == "run":
        asyncio.run(run_server(args))
    elif args.command == "multi":
        asyncio.run(run_multiple_servers())
    elif args.command == "examples":
        print("Example configurations:")
        print()
        print("1. Run a simple echo agent:")
        print("   python run_server.py run --name 'My Agent' --port 8000")
        print()
        print("2. Run with Redis message broker:")
        print("   python run_server.py run --redis")
        print()
        print("3. Run multiple agents for testing:")
        print("   python run_server.py multi")
        print()
        print("4. Environment variables:")
        print("   A2A_HOST=0.0.0.0")
        print("   A2A_PORT=8000")
        print("   A2A_REDIS_URL=redis://localhost:6379")
        print("   A2A_AUTH_ENABLED=true")
        print("   A2A_AUTH_TOKENS=agent1:token123,agent2:token456")
        print("   A2A_LOG_LEVEL=INFO")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()