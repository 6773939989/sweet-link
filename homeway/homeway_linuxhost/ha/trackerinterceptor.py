import os
import json
import time
import uuid
import logging

class TrackerInterceptor:
    """
    Subscribes to internal Home Assistant events to intercept newly created device trackers (e.g. from the Companion App).
    It then maps the device's origin user via HA core storage and associates the tracker with the specific Person entity.
    """
    
    def __init__(self, logger: logging.Logger, ha_connection):
        self.Logger = logger
        self.HaConnection = ha_connection
        self.ConfigPath = "/config"
        
        # Test common local mapping fallbacks if running in odd environments
        if os.path.exists("/homeassistant_config/.storage"):
            self.ConfigPath = "/homeassistant_config"

    def HandleEntityRegistryUpdate(self, payload: dict):
        action = payload.get("action")
        # We listen mostly for 'create', but mobile_app might trigger 'update' if restored
        if action not in ["create", "update"]:
            return
            
        entity_id = payload.get("entity_id", "")
        
        # Only care about GPS tracked devices
        if not entity_id.startswith("device_tracker."):
            return
            
        device_id = payload.get("device_id")
        if not device_id:
            return
            
        # Ignore non-mobile app entities if possible (if source info exists)
        platform = payload.get("platform", "")
        if platform and platform != "mobile_app":
            return
            
        # Run association asynchronously to not block event loop
        import threading
        t = threading.Thread(target=self._process_tracker_link, args=(entity_id, device_id), daemon=True)
        t.start()
        
    def _process_tracker_link(self, entity_id: str, device_id: str):
        self.Logger.info(f"[TrackerInterceptor] Detected new device_tracker: {entity_id} with device ID {device_id}")
        
        # 1. Trovare a quale user_id appartiene il device leggendo .storage
        mobile_app_file = os.path.join(self.ConfigPath, ".storage", "mobile_app")
        target_user_id = None
        
        # Potrebbe volerci un pochino prima che HA scriva fisicamente nel file in seguito alla registrazione
        time.sleep(1.0) 
        
        if not os.path.exists(mobile_app_file):
            self.Logger.warning(f"[TrackerInterceptor] Impossibile associare automaticamente {entity_id}: File .storage/mobile_app non accessibile.")
            return
            
        try:
            with open(mobile_app_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            entries = data.get('data', {}).get('deleted_data', []) if isinstance(data, dict) else []
            # In HA 2024+, the entries might just be 'data' list, not nested
            main_data = data.get('data', [])
            if isinstance(main_data, list):
                entries = main_data
            elif isinstance(main_data, dict) and 'deleted_data' not in data.get('data', {}):
                # Another format variation fallback
                if 'entries' in main_data:
                    entries = main_data['entries']
            
            for entry in entries:
                if entry.get("device_id") == device_id:
                    target_user_id = entry.get("user_id")
                    break
                    
        except Exception as e:
            self.Logger.error(f"[TrackerInterceptor] Eccezione leggendo mobile_app storage: {e}")
            return
            
        if not target_user_id:
            self.Logger.info(f"[TrackerInterceptor] Nessun user_id associato al device_id {device_id} nel registro mobile_app.")
            return
            
        self.Logger.info(f"[TrackerInterceptor] Il device_tracker appartiene all'utente con ID: {target_user_id}")
        
        # 2. Ottenere la lista delle Person e trovare l'ID della Person giusta
        list_response = self.HaConnection.SendAndReceiveMsg({"type": "person/list"})
        
        if not list_response or not list_response.get("success"):
            self.Logger.error("[TrackerInterceptor] Fallito recupero lista Person da Home Assistant.")
            return
            
        persons = list_response.get("result", {}).get("persons", [])
        target_person = None
        
        for person in persons:
            if person.get("user_id") == target_user_id:
                target_person = person
                break
                
        if not target_person:
            self.Logger.info(f"[TrackerInterceptor] Nessuna entità Person configurata in HA corrispondente allo user_id {target_user_id}. Binding annullato.")
            return
            
        person_id = target_person.get("id")
        current_trackers = target_person.get("device_trackers", [])
        person_name = target_person.get("name", "Unknown")
        
        # 3. Aggiornare l'entità Person iniettando il device tracker
        if entity_id in current_trackers:
            self.Logger.info(f"[TrackerInterceptor] Il device {entity_id} è già assegnato a {person_name}. Nessuna operazione necessaria.")
            return
            
        current_trackers.append(entity_id)
        
        self.Logger.info(f"[TrackerInterceptor] Invio chiamata di Binding Injections per collegare {entity_id} -> Person:{person_name}")
        
        update_response = self.HaConnection.SendAndReceiveMsg({
            "type": "person/update",
            "person_id": person_id,
            "device_trackers": current_trackers
        })
        
        if update_response and update_response.get("success"):
            self.Logger.info(f"[TrackerInterceptor] ✨ BINDING AVVENUTO CON SUCCESSO, Zero Touch! {person_name} ha ottenuto il tracker.")
        else:
            self.Logger.error(f"[TrackerInterceptor] Binding HA WebSocket fallito. Response: {update_response}")

