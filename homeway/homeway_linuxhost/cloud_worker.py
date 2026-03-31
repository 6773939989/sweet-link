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
        self.sio.on('command_update_user', self._on_update_user)
        self.sio.on('command_delete_user', self._on_delete_user)

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

    def _remove_tracked_user(self, user_id):
        try:
            tracked = self._get_tracked_users()
            if user_id in tracked:
                tracked.remove(user_id)
                path = os.path.join(self.storage_dir, 'sweetplace_users.json')
                with open(path, 'w') as f:
                    json.dump(tracked, f)
        except Exception as e:
            self.logger.error(f"[CloudWorker] Warning: Failed to remove tracked user: {e}")

    def _on_fetch_users(self, data):
        request_id = data.get('requestId')
        self.logger.info(f"[CloudWorker] Requested HA Users by Cloud. Request ID: {request_id}")
        
        try:
            if not self.ha_connection:
                raise Exception("HA WebSocket non inizializzato nel Worker")
                
            response = self.ha_connection.SendAndReceiveMsg({"type": "person/list"})
            if not response or not response.get('success', False):
                err_msg = response.get('error', {}).get('message', 'Unknown Error') if response else 'Timeout Or Disconnected'
                raise Exception(f"Failed to fetch persons from HA WebSocket: {err_msg}")
            
            all_persons = response.get('result', [])
            if isinstance(all_persons, dict):
                # HA occasionally returns dicts for single results or legacy endpoints
                all_persons = list(all_persons.values())
            elif not isinstance(all_persons, list):
                all_persons = []
                
            tracked_users = self._get_tracked_users()
            filtered_users = []
            
            for p in all_persons:
                if not isinstance(p, dict): continue
                
                person_id = p.get('id')
                if not person_id or person_id not in tracked_users:
                    continue
                    
                filtered_users.append({
                    "id": person_id,
                    "auth_id": p.get('user_id'),
                    "name": p.get('name', 'Utente Sconosciuto')
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
                
            # STEP 1: Creazione Utente di Sistema (NON Amministratore)
            auth_response = self.ha_connection.SendAndReceiveMsg({
                "type": "config/auth/create",
                "name": name,
                "group_ids": ["system-users"]
            })
            
            if not auth_response or not auth_response.get('success', False):
                err_msg = auth_response.get('error', {}).get('message', 'Unknown Error') if auth_response else 'Timeout Or Disconnected'
                raise Exception(f"Failed to create System User via HA WebSocket: {err_msg}")
                
            auth_result_raw = auth_response.get('result', {})
            user_data = auth_result_raw.get('user', auth_result_raw) if isinstance(auth_result_raw, dict) else {}
            auth_user_id = user_data.get('id')
            
            if not auth_user_id:
                raise Exception("System User creato ma ID mancante nella risposta!")
                
            # STEP 2: Creazione Persona Esplicita collegata allo User e assegnabile a dispositivi
            person_response = self.ha_connection.SendAndReceiveMsg({
                "type": "person/create",
                "name": name,
                "user_id": auth_user_id,
                "device_trackers": []
            })
            
            if not person_response or not person_response.get('success', False):
                err_msg = person_response.get('error', {}).get('message', 'Unknown Error') if person_response else 'Timeout Or Disconnected'
                raise Exception(f"Auth Success, but Person creation via HA WebSocket failed: {err_msg}")
                
            person_result_raw = person_response.get('result', {})
            person_data = person_result_raw.get('person', person_result_raw) if isinstance(person_result_raw, dict) else {}
            person_id = person_data.get('id', auth_user_id) # Fallback su auth id
            
            # Tracciamo l'ID persona generato
            self._add_tracked_user(person_id)
                
            self.logger.info(f"[CloudWorker] Successfully orchestrated User '{name}' -> Person '{person_id}' in HA.")
            
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

    def _on_update_user(self, data):
        request_id = data.get('requestId')
        person_id = data.get('person_id')
        auth_id = data.get('auth_id')
        new_name = data.get('new_name', '')
        self.logger.info(f"[CloudWorker] Requested User Update: {person_id} -> {new_name}")

        try:
            if not self.ha_connection:
                raise Exception("HA WebSocket non inizializzato")
            
            # Auth alias update
            if auth_id:
                self.ha_connection.SendAndReceiveMsg({
                    "type": "config/auth/update",
                    "user_id": auth_id,
                    "name": new_name
                })
            
            # Person layer update
            person_response = self.ha_connection.SendAndReceiveMsg({
                "type": "person/update",
                "person_id": person_id,
                "name": new_name
            })
            
            if not person_response or not person_response.get('success', False):
                err_msg = person_response.get('error', {}).get('message', 'Unknown Error') if person_response else 'Timeout'
                raise Exception(f"Failed to update User via HA WebSocket: {err_msg}")

            self.sio.emit('command_update_user_result', {
                'requestId': request_id, 'success': True, 'error': None
            })
        except Exception as e:
            self.logger.error(f"[CloudWorker] Error updating user: {str(e)}")
            self.sio.emit('command_update_user_result', {
                'requestId': request_id, 'success': False, 'error': str(e)
            })

    def _on_delete_user(self, data):
        request_id = data.get('requestId')
        person_id = data.get('person_id')
        auth_id = data.get('auth_id')
        self.logger.info(f"[CloudWorker] Requested User Deletion: {person_id} / Auth: {auth_id}")

        try:
            if not self.ha_connection:
                raise Exception("HA WebSocket non inizializzato")
                
            self.logger.info('Purging Person Layer...')
            self.ha_connection.SendAndReceiveMsg({
                "type": "person/delete",
                "person_id": person_id
            })

            if auth_id:
                self.logger.info('Purging System Auth Layer...')
                self.ha_connection.SendAndReceiveMsg({
                    "type": "config/auth/delete",
                    "user_id": auth_id
                })

            self._remove_tracked_user(person_id)
            self.logger.info(f"[CloudWorker] Successfully expunged {person_id}")

            self.sio.emit('command_delete_user_result', {
                'requestId': request_id, 'success': True, 'error': None
            })
        except Exception as e:
            self.logger.error(f"[CloudWorker] Error deleting user: {str(e)}")
            self.sio.emit('command_delete_user_result', {
                'requestId': request_id, 'success': False, 'error': str(e)
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
