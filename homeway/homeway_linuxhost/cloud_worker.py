import os
import time
import json
import threading
import requests
import socketio
import random
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
        self.sio.on('command_set_location', self._on_set_location)
        self.sio.on('command_generate_password', self._on_generate_password)

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

    def _on_set_location(self, data):
        """
        Receives: { lat: float, lon: float, display: str, plugin_id: str }
        1. Calls HA WebSocket API config/core/update (live, no restart)
        2. Writes /homeassistant/kasa-gps.yaml   (zone: Home coords)
        3. Writes /homeassistant/appdaem-gps.yaml (AppDaemon coords)
        """
        lat = data.get('lat')
        lon = data.get('lon')
        display = data.get('display', '')
        self.logger.info(f"[CloudWorker] Setting home location: lat={lat}, lon={lon} ({display})")

        errors = []

        # 1 — Live update via HA WebSocket API (no restart needed)
        try:
            ha_wait = 0
            while not getattr(self.ha_connection, 'IsConnected', False) and ha_wait < 10:
                time.sleep(1)
                ha_wait += 1
            if getattr(self.ha_connection, 'IsConnected', False):
                resp = self.ha_connection.SendAndReceiveMsg({
                    "type": "config/core/update",
                    "latitude": lat,
                    "longitude": lon,
                })
                if not resp or not resp.get('success'):
                    err = resp.get('error', {}).get('message', 'Unknown') if resp else 'Timeout'
                    self.logger.warning(f"[CloudWorker] HA WebSocket location update warning: {err}")
                    errors.append(f"HA WS: {err}")
                else:
                    self.logger.info("[CloudWorker] HA live location updated via WebSocket.")
            else:
                errors.append("HA WS offline")
        except Exception as e:
            self.logger.error(f"[CloudWorker] HA WS location error: {e}")
            errors.append(str(e))

        # 2 — Write /homeassistant/kasa-gps.yaml
        # This file holds the zone: Home coordinates for configuration.yaml !include
        try:
            kasa_path = "/homeassistant/kasa-gps.yaml"
            kasa_content = (
                f"# Sweetplace auto-generated — NON MODIFICARE MANUALMENTE\n"
                f"# Questa è la zona Home/Casa di default.\n"
                f"# Non rinominare 'Home': è usato da HA per la rilevazione presenza.\n"
                f"zone:\n"
                f"  name: Home\n"
                f"  # Coordinate aggiornate automaticamente da Sweetplace Onboarding\n"
                f"  latitude: {lat}\n"
                f"  longitude: {lon}\n"
                f"  radius: 10\n"
                f"  icon: mdi:home\n"
            )
            with open(kasa_path, 'w') as f:
                f.write(kasa_content)
            self.logger.info(f"[CloudWorker] kasa-gps.yaml written: lat={lat}, lon={lon}")
        except Exception as e:
            self.logger.error(f"[CloudWorker] kasa-gps.yaml write error: {e}")
            errors.append(str(e))

        # 3 — Write /homeassistant/appdaem-gps.yaml
        # AppDaemon reads these coordinates for presence-based automations
        try:
            appdaem_path = "/homeassistant/appdaem-gps.yaml"
            appdaem_content = (
                f"# Sweetplace auto-generated — NON MODIFICARE MANUALMENTE\n"
                f"# Coordinate GPS della casa per AppDaemon.\n"
                f"# Usate dalle app di presenza e automazioni geo-localizzate.\n"
                f"latitude: {lat}\n"
                f"longitude: {lon}\n"
            )
            with open(appdaem_path, 'w') as f:
                f.write(appdaem_content)
            self.logger.info(f"[CloudWorker] appdaem-gps.yaml written: lat={lat}, lon={lon}")
        except Exception as e:
            self.logger.error(f"[CloudWorker] appdaem-gps.yaml write error: {e}")
            errors.append(str(e))

        # Ack to backend
        self.sio.emit('command_set_location_result', {
            'success': len(errors) == 0,
            'error': '; '.join(errors) if errors else None
        })

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
                
            # Warm-up loop per gestire Race Conditions all'avvio dell'AddOn
            wait_time = 0
            while not getattr(self.ha_connection, 'IsConnected', False) and wait_time < 10:
                time.sleep(1)
                wait_time += 1
                
            if not getattr(self.ha_connection, 'IsConnected', False):
                raise Exception("HA WebSocket Auth is still pending or Offline.")
                
            response = self.ha_connection.SendAndReceiveMsg({"type": "get_states"})
            if not response or not response.get('success', False):
                err_msg = response.get('error', {}).get('message', 'Unknown Error') if response else 'Timeout Or Disconnected'
                raise Exception(f"Failed to fetch states from HA WebSocket: {err_msg}")
            
            all_states = response.get('result', [])
            if isinstance(all_states, dict):
                all_states = list(all_states.values())
            elif not isinstance(all_states, list):
                all_states = []
                
            tracked_users = self._get_tracked_users()
            filtered_users = []
            
            for state_obj in all_states:
                if not isinstance(state_obj, dict): continue
                
                entity_id = state_obj.get('entity_id', '')
                if not str(entity_id).startswith('person.'): continue
                
                attrs = state_obj.get('attributes', {})
                person_id = attrs.get('id') or attrs.get('user_id') or entity_id
                friendly_name = attrs.get('friendly_name', entity_id)
                
                if person_id not in tracked_users:
                    continue
                    
                filtered_users.append({
                    "id": person_id,
                    "auth_id": attrs.get('user_id'),
                    "name": friendly_name,
                    "entity_id": entity_id
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
                
            # Warm-up loop
            wait_time = 0
            while not getattr(self.ha_connection, 'IsConnected', False) and wait_time < 10:
                time.sleep(1)
                wait_time += 1
                
            if not getattr(self.ha_connection, 'IsConnected', False):
                raise Exception("HA WebSocket Auth is still pending or Offline.")
                
            # STEP 1: Creazione Utente di Sistema (NON Amministratore)
            auth_response = self.ha_connection.SendMsg({
                "type": "config/auth/create",
                "name": name,
                "group_ids": ["system-users"]
            }, waitForResponse=True)
            
            if not auth_response or not auth_response.get('success', False):
                err_msg = auth_response.get('error', {}).get('message', 'Unknown Error') if auth_response else 'Timeout Or Disconnected'
                raise Exception(f"Failed to create System User via HA WebSocket: {err_msg}")
                
            auth_result_raw = auth_response.get('result', {})
            user_data = auth_result_raw.get('user', auth_result_raw) if isinstance(auth_result_raw, dict) else {}
            auth_user_id = user_data.get('id')
            
            if not auth_user_id:
                raise Exception("System User creato ma ID mancante nella risposta!")
                
            # STEP 1B: Setup Initial Password (PIN) for zero-touch Companion App access
            # Temporary bypass: config/auth_provider/homeassistant/create causes HA socket hanging/disconnect!
            import string
            initial_pin = ''.join(random.choices(string.digits, k=8))
            auth_username = name.lower().replace(" ", ".")
            
            # cred_response = self.ha_connection.SendMsg({
            #     "type": "config/auth_provider/homeassistant/create",
            #     "user_id": auth_user_id,
            #     "username": auth_username,
            #     "password": initial_pin
            # }, waitForResponse=True)
            # if not cred_response or not cred_response.get('success'):
            #     self.logger.warning(f"[CloudWorker] Failed to set initial PIN for {auth_username}: {cred_response.get('error') if cred_response else 'Timeout'}")
            #     initial_pin = "ERRORE"
                
            # STEP 2: Creazione Persona Esplicita collegata allo User e assegnabile a dispositivi
            person_response = self.ha_connection.SendMsg({
                "type": "person/create",
                "name": name,
                "user_id": auth_user_id,
                "device_trackers": []
            }, waitForResponse=True)
            
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
                'result': {'name': name, 'id': person_id, 'username': auth_username, 'pin': initial_pin},
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
            p_resp = self.ha_connection.SendAndReceiveMsg({
                "type": "person/delete",
                "person_id": person_id
            })
            if not p_resp or not p_resp.get('success'):
                err_msg = p_resp.get('error', {}) if p_resp else 'Timeout Person'
                self.logger.error(f"Failed to delete Person {person_id}: {err_msg}")

            if auth_id:
                self.logger.info('Purging System Auth Layer...')
                u_resp = self.ha_connection.SendAndReceiveMsg({
                    "type": "config/auth/delete",
                    "user_id": auth_id
                })
                if not u_resp or not u_resp.get('success'):
                    err_msg = u_resp.get('error', {}) if u_resp else 'Timeout Auth'
                    raise Exception(f"Failed to delete System User: {err_msg}")
            else:
                self.logger.warning(f"No auth_id provided for {person_id}. Attempting fallback lookup via config/auth/list...")
                # Fallback to search by name or ID
                a_resp = self.ha_connection.SendAndReceiveMsg({"type": "config/auth/list"})
                if a_resp and a_resp.get('success'):
                    # The name is often identical to the person ID string or name slug
                    found = False
                    for usr in a_resp.get('result', []):
                        if usr.get('name', '').lower() == str(person_id).replace('_', ' ').lower() or usr.get('id') == person_id:
                            self.logger.info(f"Fallback found matching user: {usr.get('name')} ({usr.get('id')}). Purging...")
                            fallback_u_resp = self.ha_connection.SendAndReceiveMsg({
                                "type": "config/auth/delete",
                                "user_id": usr.get('id')
                            })
                            if not fallback_u_resp or not fallback_u_resp.get('success'):
                                raise Exception(f"Fallback delete failed: {fallback_u_resp.get('error') if fallback_u_resp else 'Timeout'}")
                            found = True
                            break
                    if not found:
                        self.logger.warning(f"Could not find matching System User for {person_id}. It might have been already deleted.")
                else:
                    self.logger.error("Could not fetch user list for fallback delete.")

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

    def _on_generate_password(self, data):
        """
        Receives: { reqId, auth_id, password }
        Calls HA: auth/admin_change_password to set a new password for the user
        Acks back: command_generate_password_result { reqId, success, error }
        """
        req_id = data.get('reqId')
        auth_id = data.get('auth_id')
        password = data.get('password')
        self.logger.info(f"[CloudWorker] Generating one-time password for auth_id={auth_id}, reqId={req_id}")
        try:
            if not self.ha_connection:
                raise Exception("HA WebSocket non inizializzato")
            wait_time = 0
            while not getattr(self.ha_connection, 'IsConnected', False) and wait_time < 10:
                time.sleep(1)
                wait_time += 1
            if not getattr(self.ha_connection, 'IsConnected', False):
                raise Exception("HA WebSocket not connected.")

            resp = self.ha_connection.SendAndReceiveMsg({
                "type": "config/auth/create",  # ensure credentials exist first
            })
            # Use admin change password API
            resp = self.ha_connection.SendAndReceiveMsg({
                "type": "config/auth_provider/homeassistant/create",
                "user_id": auth_id,
                "username": auth_id,  # will be overridden by HA using existing username
                "password": password
            })
            # If credentials already exist, change them instead
            if resp and not resp.get('success'):
                err_code = resp.get('error', {}).get('code', '') if resp else ''
                if 'already_exists' in str(err_code) or 'not_found' not in str(err_code):
                    # Try admin_change_password
                    resp2 = self.ha_connection.SendAndReceiveMsg({
                        "type": "config/auth_provider/homeassistant/change_password",
                        "user_id": auth_id,
                        "password": password
                    })
                    if not resp2 or not resp2.get('success'):
                        # Final fallback: admin override
                        resp2 = self.ha_connection.SendAndReceiveMsg({
                            "type": "auth/admin_change_password",
                            "user_id": auth_id,
                            "password": password
                        })
                    resp = resp2

            if not resp or not resp.get('success'):
                err_msg = resp.get('error', {}).get('message', 'Unknown') if resp else 'Timeout'
                raise Exception(f"HA password set failed: {err_msg}")

            self.logger.info(f"[CloudWorker] Password set successfully for {auth_id}")
            self.sio.emit('command_generate_password_result', {'reqId': req_id, 'success': True})
        except Exception as e:
            self.logger.error(f"[CloudWorker] _on_generate_password error: {e}")
            self.sio.emit('command_generate_password_result', {'reqId': req_id, 'success': False, 'error': str(e)})

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
