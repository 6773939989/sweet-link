import os
import time
import json
import threading
import requests
import socketio
import urllib3

# Disabilita gli InsecureRequestWarning quando chiamiamo HTTPS interni (se usati)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class CloudWorker:
    def __init__(self):
        self._thread = None
        self._running = False
        self.logger = None
        self.plugin_id = None
        self.private_key = None
        self.sio = socketio.Client(reconnection=True, reconnection_delay=5, reconnection_delay_max=30)
        
        # Registra i listener del SocketIO
        self.sio.on('connect', self._on_connect)
        self.sio.on('disconnect', self._on_disconnect)
        self.sio.on('command_fetch_users', self._on_fetch_users)
        self.sio.on('command_create_user', self._on_create_user)

    def Start(self, logger, plugin_id, private_key, ha_connection, storage_dir):
        self.logger = logger
        self.plugin_id = plugin_id
        self.private_key = private_key
        self.ha_connection = ha_connection
        self.storage_dir = storage_dir
        self.logger.info("Starting Secure Cloud Worker Demon for Zero-Touch Provisioning...")
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def Stop(self):
        self._running = False
        if self.sio.connected:
            self.sio.disconnect()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _get_ha_headers(self):
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def _get_ha_api_url(self):
        # L'URL di root per parlare col core da un AddOn
        return "http://supervisor/core/api"

    def _on_connect(self):
        self.logger.info("[CloudWorker] Securely Authenticated to Sweetplace Cloud WebSocket")

    def _on_disconnect(self):
        self.logger.warning("[CloudWorker] Disconnected from Sweetplace Cloud WebSocket")

    def _get_tracked_users(self):
        try:
            path = os.path.join(self.storage_dir, 'sweetplace_users.json')
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
        except Exception: pass
        return []

    def _add_tracked_user(self, user_id):
        try:
            tracked = self._get_tracked_users()
            if user_id not in tracked:
                tracked.append(user_id)
                path = os.path.join(self.storage_dir, 'sweetplace_users.json')
                with open(path, 'w') as f:
                    json.dump(tracked, f)
        except Exception as e:
            self.logger.error(f"[CloudWorker] Warning: Failed to save tracked user: {e}")

    def _on_fetch_users(self, data):
        request_id = data.get('requestId')
        self.logger.info(f"[CloudWorker] Requested HA Users by Cloud. Request ID: {request_id}")
        
        try:
            if not self.ha_connection:
                raise Exception("HA WebSocket non inizializzato nel Worker")
                
            response = self.ha_connection.SendAndReceiveMsg({"type": "config/auth/list"})
            if not response or not response.get('success', False):
                err_msg = response.get('error', {}).get('message', 'Unknown Error') if response else 'Timeout Or Disconnected'
                raise Exception(f"Failed to fetch users from HA WebSocket: {err_msg}")
            
            all_users = response.get('result', [])
            if not isinstance(all_users, list):
                all_users = []
                
            tracked_users = self._get_tracked_users()
            filtered_users = []
            
            for u in all_users:
                if not isinstance(u, dict): continue
                
                user_id = u.get('id')
                friendly_name = u.get('name', 'Sconosciuto')
                
                if user_id not in tracked_users:
                    continue
                    
                filtered_users.append({
                    "id": user_id,
                    "name": friendly_name,
                    "entity_id": f"user.{user_id}"
                })
                
            self.logger.info(f"[CloudWorker] Found {len(filtered_users)} standard users. Sending to Cloud.")
            self.sio.emit('command_fetch_users_result', {
                'requestId': request_id, 
                'users': filtered_users,
                'error': None
            })
        except Exception as e:
            self.logger.error(f"[CloudWorker] Error fetching users via HA Socket: {str(e)}")
            self.sio.emit('command_fetch_users_result', {
                'requestId': request_id, 
                'users': [],
                'error': f"Home Assistant Local WebSocket API Error: {str(e)}"
            })

    def _on_create_user(self, data):
        request_id = data.get('requestId')
        user_data = data.get('user_data', {})
        name = user_data.get('name', 'Nuovo Utente')
        self.logger.info(f"[CloudWorker] Requested User Creation by Cloud: {name}")

        try:
            if not self.ha_connection:
                raise Exception("HA WebSocket non inizializzato")
                
            response = self.ha_connection.SendAndReceiveMsg({
                "type": "config/auth/create",
                "name": name
            })
            
            if not response or not response.get('success', False):
                err_msg = response.get('error', {}).get('message', 'Unknown Error') if response else 'Timeout Or Disconnected'
                raise Exception(f"Failed to create user via HA WebSocket: {err_msg}")
                
            result_data_raw = response.get('result', {})
            result_data = result_data_raw.get('user', result_data_raw) if isinstance(result_data_raw, dict) else {}
            person_id = result_data.get('id', 'unknown_id')
            self._add_tracked_user(person_id)
                
            self.logger.info(f"[CloudWorker] Successfully created person '{name}' [{person_id}] in Home Assistant.")
            
            self.sio.emit('command_create_user_result', {
                'requestId': request_id, 
                'success': True,
                'result': {'name': name, 'id': person_id},
                'error': None
            })
        except Exception as e:
            self.logger.error(f"[CloudWorker] Error creating user: {str(e)}")
            self.sio.emit('command_create_user_result', {
                'requestId': request_id, 
                'success': False,
                'error': str(e)
            })

    def _run_loop(self):
        cloud_url = "https://sweetplace-starthere.up.railway.app"
        self.logger.info(f"[CloudWorker] Connecting to {cloud_url}...")
        
        while self._running:
            try:
                if not self.sio.connected:
                    self.sio.connect(cloud_url, transports=['websocket', 'polling'], auth={
                        'plugin_id': self.plugin_id,
                        'private_key': self.private_key
                    })
            except Exception as e:
                self.logger.warning(f"[CloudWorker] Connection to cloud failed, retrying in 10s... ({str(e)})")
                
            time.sleep(10)
            
# Globale Singleton
CloudWorkerInstance = CloudWorker()
