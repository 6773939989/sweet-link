import os
import time
import subprocess
import threading
import logging
import requests
import json
from enum import Enum

class CloudflareStatus(Enum):
    STOPPED = 1
    REQUESTING_TOKEN = 2
    RETRY_WAIT = 3
    RUNNING = 4

class CloudflareManager:
    """
    Manages the lifecycle of the zero-trust cloudflared daemon.
    Requests a token from the Sweetplace Backend API upon boot, and passes it to the local CLI binary.
    """
    
    def __init__(self, logger: logging.Logger):
        self.Logger = logger
        self.Status = CloudflareStatus.STOPPED
        self.Lock = threading.Lock()
        
        self.Thread = None
        self.Subprocess = None
        
        self.MacAddress = None
        self.PluginId = None
        self.ApiUrl = None
        
        self._shutdown_event = threading.Event()
        
        # Verify binary exists (downloaded by Dockerfile)
        self.BinaryPath = "/usr/local/bin/cloudflared"

    def Start(self, api_url: str, mac_address: str = None, plugin_id: str = None):
        with self.Lock:
            if self.Thread is not None:
                return
            
            if not mac_address and not plugin_id:
                self.Logger.error("[CloudflareManager] Cannot start without a mac_address or plugin_id")
                return
            
            self.MacAddress = mac_address
            self.PluginId = plugin_id
            self.ApiUrl = api_url
            self.Status = CloudflareStatus.REQUESTING_TOKEN
            self._shutdown_event.clear()
            
            self.Thread = threading.Thread(target=self._run_loop, name="CloudflareManagerThread", daemon=True)
            self.Thread.start()
            self.Logger.info("[CloudflareManager] Orchestration thread started.")

    def Stop(self):
        with self.Lock:
            if self.Thread is None:
                return
            
            self.Logger.info("[CloudflareManager] Stopping Zero Trust orchestration...")
            self._shutdown_event.set()
            
            if self.Subprocess is not None:
                try:
                    self.Subprocess.terminate()
                    self.Subprocess.wait(timeout=5)
                except Exception as e:
                    self.Logger.error(f"[CloudflareManager] Failed to gracefully terminate cloudflared: {e}")
                finally:
                    self.Subprocess = None

            self.Status = CloudflareStatus.STOPPED
            self.Thread = None

    def _run_loop(self):
        if not os.path.exists(self.BinaryPath):
            self.Logger.error(f"[CloudflareManager] CRITICAL ERROR: cloudflared binary not found at {self.BinaryPath}. Check Dockerfile build.")
            return

        while not self._shutdown_event.is_set():
            token = self._request_token()
            
            if not token:
                # If token was not retrieved (e.g. backend error, or backend not configured)
                self.Logger.warning("[CloudflareManager] Token not retrieved. Retrying in 60s...")
                self.Status = CloudflareStatus.RETRY_WAIT
                self._shutdown_event.wait(60)
                continue
                
            # If backend explicitly returned empty but HTTP 200/503 (feature disabled)
            if token == "__DISABLED__":
                self.Logger.info("[CloudflareManager] Cloudflare integration disabled by backend. Manager sleeping indefinitely.")
                self.Status = CloudflareStatus.STOPPED
                break
                
            # Token received. Spawn the daemon.
            self.Logger.info("[CloudflareManager] Tunnel Token acquired. Spawning cloudflared daemon...")
            self.Status = CloudflareStatus.RUNNING
            
            try:
                # Spawn cloudflared run --token [TOKEN] --no-autoupdate
                self.Subprocess = subprocess.Popen(
                    [self.BinaryPath, "tunnel", "--no-autoupdate", "run", "--token", token],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1
                )
                
                # Consume output silently or log warnings
                for line in self.Subprocess.stdout:
                    if self._shutdown_event.is_set():
                        break
                    
                    if "Registered tunnel connection" in line:
                        self.Logger.info("[CloudflareManager] TUNNEL CONNECTION SECURED & ACTIVE.")
                    elif "ERR" in line:
                        self.Logger.error(f"[cloudflared] {line.strip()}")
                
                # Wait for subprocess finish
                self.Subprocess.wait()
                self.Subprocess = None
                
            except Exception as e:
                self.Logger.error(f"[CloudflareManager] Subprocess runtime exception: {e}")
            
            if not self._shutdown_event.is_set():
                self.Logger.warning("[CloudflareManager] cloudflared daemon exited unexpectedly. Respawning in 10s...")
                self._shutdown_event.wait(10)


    def _request_token(self) -> str:
        self.Status = CloudflareStatus.REQUESTING_TOKEN
        endpoint = f"{self.ApiUrl}/api/cloudflare/provision"
        
        try:
            # Prefer plugin_id over mac_address for multi-NIC reliability
            if self.PluginId:
                payload = {"plugin_id": self.PluginId}
            else:
                payload = {"mac_address": self.MacAddress}
            
            res = requests.post(endpoint, json=payload, timeout=15)
            if res.status_code == 200:
                data = res.json()
                return data.get("token", "")
            elif res.status_code == 503:
                # Disabled administratively
                return "__DISABLED__"
            else:
                self.Logger.error(f"[CloudflareManager] Backend refused token request: HTTP {res.status_code}")
                return None
                
        except Exception as e:
            self.Logger.error(f"[CloudflareManager] Network error contacting {endpoint}: {e}")
            return None
