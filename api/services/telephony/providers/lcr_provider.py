"""
LCR implementation of the TelephonyProvider interface using SIP.
"""

import asyncio
import uuid
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from pyVoIP.VoIP import VoIPPhone, CallState, InvalidStateError

from api.services.telephony.base import (
    CallInitiationResult,
    NormalizedInboundData,
    TelephonyProvider,
)
from api.utils.common import get_backend_endpoints

if TYPE_CHECKING:
    from fastapi import WebSocket

# Disable verbose pyVoIP logging unless debugging
logging.getLogger("pyVoIP").setLevel(logging.WARNING)

class LCRProvider(TelephonyProvider):
    """
    LCR implementation of TelephonyProvider using pyVoIP for SIP signaling.
    """

    PROVIDER_NAME = "lcr"
    WEBHOOK_ENDPOINT = "lcr"
    
    # Shared executor for synchronous SIP operations
    _executor = ThreadPoolExecutor(max_workers=5)

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize LCRProvider with configuration.
        """
        self.host = config.get("host", "trunksip11.lcrcom.es")
        self.port = int(config.get("port", 5060))
        # Maps internal 'auth_token' to SIP Password and 'api_key' to SIP Username
        self.password = config.get("auth_token") 
        self.username = config.get("api_key")
        self.from_numbers = config.get("from_numbers", [])

        if isinstance(self.from_numbers, str):
            self.from_numbers = [self.from_numbers]
            
        self.sip_client: Optional[VoIPPhone] = None

    def _get_sip_client(self) -> VoIPPhone:
        """
        Lazy initialization of the SIP client.
        Note: pyVoIP binds to a port (default 5060). In a scaled env, this is tricky.
        In Docker, we must ensure nothing else binds 5060 or use a random port.
        """
        if not self.sip_client:
            if not self.validate_config():
                raise ValueError("LCR SIP credentials missing")
            
            logger.info(f"Initializing SIP Client for {self.username}@{self.host}...")
            # We use a random local port to avoid conflicts if multiple workers run
            # but standard SIP might require 5060. For outbound, ephemeral is usually fine.
            self.sip_client = VoIPPhone(
                self.host,
                self.port,
                self.username,
                self.password,
                myIP="0.0.0.0", # Listern on all interfaces
                callCallback=self._on_call_event
            )
            # Start the SIP client thread
            self.sip_client.start()
            logger.success("SIP Client started")
            
        return self.sip_client
        
    def _on_call_event(self, call):
        """
        Callback for SIP events.
        """
        logger.info(f"SIP Call Event: {call.state} - {call.info()}")

    def _do_sip_call(self, to_number: str) -> str:
        """
        Synchronous wrapper for dialing.
        """
        phone = self._get_sip_client()
        try:
            call = phone.call(to_number)
            return str(call.callID)
        except Exception as e:
            logger.error(f"SIP Dial Error: {e}")
            raise

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        """
        Initiate an outbound call via SIP.
        """
        logger.info(f"Initiating LCR SIP call to {to_number} using {self.username}...")
        
        loop = asyncio.get_running_loop()
        try:
            # Run blocking SIP call in thread
            call_id = await loop.run_in_executor(
                self._executor, 
                self._do_sip_call, 
                to_number
            )
            
            return CallInitiationResult(
                call_id=call_id,
                status="ringing", # Optimistic status
                provider="lcr",
                metadata={"sip_host": self.host}
            )
        except Exception as e:
            logger.error(f"Failed to initiate SIP call: {e}")
            raise

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        # pyVoIP doesn't easily expose call lookup by ID after the fact without tracking
        # For now return optimistic status
        return {"status": "in-progress", "call_id": call_id}

    async def get_available_phone_numbers(self) -> List[str]:
        return self.from_numbers

    def validate_config(self) -> bool:
        return bool(self.host and self.password and self.username)

    async def verify_webhook_signature(
        self, url: str, params: Dict[str, Any], signature: str
    ) -> bool:
        return True

    async def get_webhook_response(
        self, workflow_id: int, user_id: int, workflow_run_id: int
    ) -> str:
        # LCR/pyVoIP doesn't use webhooks for media. 
        # This is strictly legacy for the abstraction.
        return ""

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        return {"cost_usd": 0.0}

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def handle_websocket(
        self,
        websocket: "WebSocket",
        workflow_id: int,
        user_id: int,
        workflow_run_id: int,
    ) -> None:
        """
        No-op for raw SIP unless we bridge RTP to this socket.
        """
        await websocket.accept()
        logger.warning("LCR SIP does not support WebSocket media bridging yet.")
        
    # ======== INBOUND ========
    @classmethod
    def can_handle_webhook(cls, webhook_data: Dict[str, Any], headers: Dict[str, str]) -> bool:
        return False

    @staticmethod
    def parse_inbound_webhook(webhook_data: Dict[str, Any]) -> NormalizedInboundData:
        raise NotImplementedError

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        return True

    def normalize_phone_number(self, phone_number: str) -> str:
        return phone_number

    async def verify_inbound_signature(self, url: str, webhook_data: Dict[str, Any], signature: str) -> bool:
        return True

    @staticmethod
    async def generate_inbound_response(websocket_url: str, workflow_run_id: int = None) -> any:
        return {}

    @staticmethod
    def generate_error_response(error_type: str, message: str) -> any:
        return {}
