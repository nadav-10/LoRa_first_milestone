from flask import Flask, jsonify, request
import time
import base64
import struct
import queue
import threading
import random
from lora_common.loramodem import LoRaModem
import argparse
import hashlib
from lora_common.pubkey import RSA1024
app = Flask(__name__)
USER_EXPIRATION_SECONDS = 300
KEY_PUBLISH_INTERVAL = 60
USER_DETAILS_INTERVAL = 60
KEY_PUBLISH_INTERVAL_OFFSET = 0
USER_DETAILS_INTERVAL_OFFSET = 0
QUEUE_MAX_SIZES = (50, 50, 50, 30)
WFQ_PRIORITY_WEIGHTS = (0.5, 0.8, 0.95) #accumulative
QUEUE_ENTRY_TTL = 300
PERIODIC_KEY_CHANCE = 0.1
PERIODIC_TEXT_CHANCE = 0.2
PERIODIC_DETAILS_CHANCE = 0.1
# users_registry stores the full object required by the /users endpoint
# key: key_id (string), value: dictionary with key_id, nick, name, phone, delay_secs, is_local
users_registry = {}
users_lock = threading.RLock()

#stores last time seen of each user
last_seen = {}
last_seen_lock = threading.RLock()

#stores all incoming messages to local users. key: key_id, value: list
message_inbox = {}
inbox_lock = threading.RLock()

#All stats for /stats endpoint
server_stats = {
    "local_messages_sent": 0,
    "global_messages_sent": 0,
    "valid_alive_requests": 0,
    "users_requests_count": 0
}
stats_lock = threading.RLock()

# Stores user details for users we haven't seen a Public Key for yet. key: sender_id (int), value: {nick, name, phone, utime}
cached_user_details = {}
cached_details_lock = threading.RLock()

# Messages waiting to be sent over the LoRa radio
#Every queue has a maximum length to prevent starvation 
# - too many messages stockpiling in one queue, making newer messages delayed
lora_out_queues = {
    1: queue.Queue(maxsize=QUEUE_MAX_SIZES[0]),  # Key Publish (Critical)
    2: queue.Queue(maxsize=50),  # Text/Plain (Important)
    3: queue.Queue(maxsize=50), # Forwarding (Less important)
    4: queue.Queue(maxsize=30)   # User Details + Key Republish (Least urgent)
}

#All keys ready to be sent in LoRa, prevents re-entering a key publish
pending_keys = set()
pending_keys_lock = threading.RLock()

#REST API BACKEND:
class ServerBE:
    """Contains all helper functions to the main HTTP requests recievers"""
    @staticmethod
    def route_http_message(msg_data : dict) -> str:
        """
        Routes a message based on the 'to' key_id.
        """
        target_key_id = msg_data.get('to')
        
        # Find if the user is local by checking if he's in the inbox
        with inbox_lock:
            if target_key_id in message_inbox:
                # LOCAL ROUTE: Put in the inbox
                message_inbox[target_key_id].append(msg_data)
                with stats_lock:
                    server_stats["local_messages_sent"] += 1
                return "local"
        # REMOTE ROUTE: Put in LoRa Priority Queue
        if not LoRaWriter.serialize_lora_message(msg_data):
            return "error"
        return "remote"
        
    @staticmethod
    def get_key_id_from_b64(key_b64 : str) -> int | None:
        """
        Extracts the 4-byte key_id from the Base64 public key.
        The first 4 bytes are the key_id.
        """
        try:
            public_bytes = base64.b64decode(key_b64)
            # Unpack the first 4 bytes as a 32-bit big-endian integer
            key_id = struct.unpack("!I", public_bytes[:4])[0]
            return key_id
        except Exception:
            return None
        
    next_publish = {}
    next_details = {}
    @staticmethod
    def handle_alive_calls(data : dict) -> list[dict]:
        """Handles all operations to be executed by /alive call"""
        key_b64 = data["key_b64"]
        key_id = ServerBE.get_key_id_from_b64(key_b64)
        with stats_lock:
            server_stats["valid_alive_requests"] += 1
        if "details_b64" in data:
            encrypted_info = base64.b64decode(data["details_b64"])
            with users_lock:
                users_registry[key_id] = {
                    "key_id": key_id,
                    "key_b64": key_b64,
                    "is_local": True      # This user is hitting our REST API directly
                }
            LoRaListener.register_user(key_id, encrypted_info, 0)
        else: encrypted_info = None
        with inbox_lock:
            message_inbox[key_id] = message_inbox.get(key_id, [])
        with last_seen_lock:
            last_seen[key_id] = time.time()
        if key_id not in ServerBE.next_publish or time.time() > ServerBE.next_publish[key_id]:
            print(f"[DEBUG] publishing {key_id}'s key")
            LoRaWriter.publish_key(key_b64, 1 if key_id not in ServerBE.next_publish else 4)
            ServerBE.next_publish[key_id] = time.time() + KEY_PUBLISH_INTERVAL + random.randint(0, KEY_PUBLISH_INTERVAL_OFFSET)
        if key_id not in ServerBE.next_details or time.time() > ServerBE.next_details[key_id]:
            print("hello world for debug")
            LoRaWriter.publish_user_details(key_id, encrypted_info)
            ServerBE.next_details[key_id] = time.time() + USER_DETAILS_INTERVAL + random.randint(0, USER_DETAILS_INTERVAL_OFFSET)
        # Retrieve messages for this specific user
        with inbox_lock:
            return message_inbox[key_id]
        
    @staticmethod
    def cleanup_inactive_users():
        """
        Triggered by /users calls. Compares last_seen_registry 
        against users_registry and prunes expired entries.
        """
        try:
            current_time = time.time()
            expired_ids = []

            with last_seen_lock:
                # Identify which users have timed out
                for key_id, last_ts in last_seen.items():
                    if current_time - last_ts > USER_EXPIRATION_SECONDS:
                        expired_ids.append(key_id)

                # Prune the data
                with users_lock, inbox_lock:
                    for key_id in expired_ids:
                        print(f"[DEBUG] Pruning inactive user {key_id}")
                        users_registry.pop(key_id, None)
                        message_inbox.pop(key_id, None)
                        last_seen.pop(key_id, None)
        except Exception as e:
            print(f"[ERROR] Unexpected error cleaning inactive users: {e}")

@app.route('/text', methods=['POST'])
@app.route('/plain', methods=['POST'])
def handle_message():
    data = request.get_json()
    
    # Validate all required fields appear
    required_fields = ['utime', 'sender', 'to']
    if not all(keys in data for keys in required_fields):
        return jsonify({"status": "error", "error": "Missing mandatory fields"}), 400

    # Ensure it has either encrypted or plain payload
    if 'crypt2_b64' not in data and 'plain2_b64' not in data:
        return jsonify({"status": "error", "error": "No message content found"}), 400

    # Route the message
    ServerBE.route_http_message(data)
    
    return jsonify({
        "status": "OK",
    })

@app.route('/alive', methods=['POST'])
def alive():
    """
    POST request representing the status of a single user.
    Updates user info and returns pending messages.
    """
    data = request.get_json()
    # key_b64 is the only mandatory field
    if not data or 'key_b64' not in data:
        return jsonify({
            "status": "error",
            "error": "Invalid key_b64"
        }), 400
    key_b64 = data.get('key_b64')
    key_id = ServerBE.get_key_id_from_b64(key_b64)
    if key_id is None:
        return jsonify({"status": "error", "error": "Malformed key_b64"}), 400
    #handle all operations related to /alive endpoint
    messages = ServerBE.handle_alive_calls(data)
    return jsonify({
        "status": "OK",
        "messages": messages
    })

@app.route('/users', methods=['GET'])
def get_users():
    """
    Returns a list of all known users (local and remote).
    As per documentation, this is a GET request returning a JSON array of objects.
    """
    ServerBE.cleanup_inactive_users()
    with stats_lock:
        server_stats["users_requests_count"] += 1
    with users_lock:
        return jsonify({
            "status": "OK",
            "users": list(users_registry.values()), 
        })

@app.route('/stats', methods=['GET'])
def get_stats():
    """
    Returns router statistics.
    """
    with users_lock, stats_lock:
        return jsonify({
            "status": "OK",
            "local_messages": server_stats["local_messages_sent"],
            "global_messages": server_stats["global_messages_sent"],
            "alive_requests": server_stats["valid_alive_requests"],
            "total_users": len(users_registry),
            "users_endpoint_calls": server_stats["users_requests_count"]
        })

#MESSAGE RECEPTION CODE
class LoRaListener:
    """Endlessly reads all incoming messages and manages them"""
    text_forwarding_counter = {}
    @staticmethod
    def periodic_text_forwarding(packet : bytes, sender : bytes, to : bytes, crypt : bytes):
        hashed_text = hashlib.sha256((sender, to, crypt))
        LoRaListener.text_forwarding_counter[hashed_text] = LoRaListener.text_forwarding_counter.get(hashed_text, default = -1) + 1
        if random.random() < PERIODIC_TEXT_CHANCE ** LoRaListener.text_forwarding_counter[hashed_text]:
            lora_out_queues[3].put(int(time.time()), packet)

    @staticmethod
    def register_user(user_id : int, details : bytes, delay : int):
        with users_lock:
            key_b64 = users_registry[user_id]["key_b64"]
        

        my_public_rsa_key = RSA1024.from_public_bytes(base64.b64decode(key_b64))
        info = my_public_rsa_key.encrypt(details)
        if info[0] == 0x00:
            return
        info = info[1:].decode("utf-8")
        parts = info.split(':') if info else ["", "", ""]
        if len(parts) != 3:
            print(f"[ERROR] Malformed user details string: '{info}'")
            return

        new_details = {
            "nick": parts[0],
            "name": parts[1],
            "phone": parts[2],
            "delay_secs": max(0, delay)
        }

        with users_lock:
            # User is known, update their details in the registry
            users_registry[user_id].update(new_details)
            print(f"[DEBUG] Updated registry for known user {user_id}")

    @staticmethod
    def handle_incoming_key_publish(packet : bytes):
        try:
            if len(packet) != 134:
                print(f"[ERROR] Received packet of length {len(packet)}, should be 134")
                return
            utime, sender_id = struct.unpack("!II", packet[2:10])
            # Extract the full 128-byte key (starts at index 6 in the packet)
            full_key_bytes = bytes(packet[6:134])
            key_b64 = base64.b64encode(full_key_bytes).decode('utf-8')
            # Update the registry as a REMOTE user
            # Remote users have is_local=False and no specific nick/name yet

            current_time = int(time.time())
            delay = max(0, current_time - utime)
            with users_lock:
                if sender_id not in users_registry:
                    users_registry[sender_id] = {
                        "key_id": sender_id,
                        "key_b64": key_b64,
                        "nick": "", 
                        "name": "",
                        "phone": "",
                        "delay_secs": delay,
                        "is_local": False 
                    }
                    lora_out_queues[3].put((int(packet[2:6]), packet))
                    print(f"[DEBUG] Registered new remote user: {sender_id}")
                    with cached_details_lock:
                        if sender_id in cached_user_details:
                            cached_data, delay = cached_user_details.pop(sender_id)
                            LoRaListener.register_user(sender_id, cached_data, delay)
                            print(f"[DEBUG] Applied cached details to new user {sender_id}")
                else:
                    users_registry[sender_id]["delay_secs"] = delay
                    if random.random < PERIODIC_KEY_CHANCE:
                        lora_out_queues[3].put((int(time.time()), packet))
                    
        except Exception as e:
            print(f"[ERROR] Unexpected error parsing Key Publish: {e}")

    @staticmethod
    def handle_incoming_text(packet : bytes):
        try:
            # Unpack the fixed header part (14 bytes)
            # !B (Header) B (Kind) I (Utime) I (Sender) I (To)
            if len(packet) < 14:
                print(f"[ERROR] Received packet of length {len(packet)}, should be at least 14")
                return
            kind, utime, sender_id, to_id = struct.unpack("!BIII", packet[1:14])

            # The rest of the packet is the payload
            payload_bytes = packet[14:]
            payload_b64 = base64.b64encode(payload_bytes).decode('utf-8')

            # Build the message object
            msg_data = {
                "utime": utime,
                "sender": sender_id,
                "to": to_id,
            }
            LoRaListener.periodic_text_forwarding(packet, sender_id, to_id, payload_bytes)

            if kind == 0x03:
                msg_data["crypt2_b64"] = payload_b64
            else:
                msg_data["plain2_b64"] = payload_b64
                print(payload_bytes.decode('utf-8'))

            print(f"[DEBUG] Received Text Kind {kind} from {sender_id} to {to_id}")
            
            # Route it (local inbox or remote queue)
            with inbox_lock:
                if to_id in message_inbox:
                    message_inbox[to_id].append(msg_data)

        except Exception as e:
            print(f"[ERROR] Unexpected error parsing incoming text: {e}")

    @staticmethod
    def handle_incoming_details(packet : bytes):
        try:
            if len(packet) != 138:
                print(f"[ERROR] Received packet of length {len(packet)}, should be 138")
                return

            utime, sender_id = struct.unpack("!II", packet[2:10])
        
            # Payload starts after the 10-byte header
            payload_bytes = packet[10:]

            with users_lock:
                if sender_id in users_registry:
                    if [users_registry[sender_id].get(field, "") for field in ["nick", "name", "phone"]] == ["", "", ""]:
                        lora_out_queues[3].put((int(time.time()), packet))
                    elif random.random() < PERIODIC_DETAILS_CHANCE:
                            lora_out_queues[3].put((int(time.time()), packet))
                    LoRaListener.register_user(sender_id, payload_bytes, int(time.time() - utime))
                else:
                    lora_out_queues[3].put((time.time(), packet))
                    # User unknown, cache details in the "Waiting Room"
                    with cached_details_lock:
                        cached_user_details[sender_id] = (payload_bytes, int(time.time() - utime))
                    print(f"[DEBUG] Cached details for unknown user {sender_id}")
        except Exception as e:
            print(f"[ERROR] Unexpected error parsing incoming user details: {e}")

    @staticmethod
    def read_incoming_messages(modem : LoRaModem):
        """
        Reads a packet from the LoRa modem and handles it.
        """
        try:
            packet = modem.read_bytes()
            if not packet:
                return
            # Check for the constant header '\xAE' and minimum length
            # Every incoming message must have: header (1), kind (1), utime (1), sender (1)
            if len(packet) < 10 or packet[0] != 0xAE:
                print("[ERROR] packet doesnt start with ae / packet is too short")
                return

            kind = packet[1]
            if kind == 0x01:  # Key publish
                LoRaListener.handle_incoming_key_publish(packet)
            elif kind in [0x03, 0x05]:
                LoRaListener.handle_incoming_text(packet)
            elif kind == 0x02:
                LoRaListener.handle_incoming_details(packet)
            else:
                print(f"[ERROR] packet kind isn't legal (1, 2, 3, 5) and instead {kind}")
                return
            sender_id = struct.unpack("!I", packet[6:10])
            with last_seen_lock:
                last_seen[sender_id] = time.time()
        except Exception as e:
            print(f"[ERROR] Unexpected error reading incoming messages: {e}")

    @staticmethod
    def lora_recieve_loop(device_path : str):
        try:
            with LoRaModem(device_path) as modem:
                while True:
                    LoRaListener.read_incoming_messages(modem)
                    time.sleep(0.1)
        except Exception as e:
            print(f"[ERROR] Unexpected error in receiving loop: {e}")

class LoRaWriter:
    """Handles inserting packets into the queue and sending the queue's elements"""
    @staticmethod
    def serialize_lora_message(msg_data : dict) -> str:
        """
        Turns a JSON message from the API into a bytes object for LoRa transmission.
        Manages both kind 5 - plain, and kind 3 - encrypted text.
        """
        try:
            if 'crypt2_b64' in msg_data:
                kind = 0x03
                payload_b64 = msg_data['crypt2_b64']
            else:
                kind = 0x05
                payload_b64 = msg_data['plain2_b64']

            payload_bytes = base64.b64decode(payload_b64)

            # AE (1 byte), Kind (1 byte), UTime (4 bytes), Sender (4 bytes), To (4 bytes)
            header = struct.pack('!BBIII', 
                                0xAE, 
                                kind, 
                                int(msg_data.get('utime', time.time())), 
                                msg_data['sender'], 
                                msg_data['to'])

            print(f"[DEBUG] Sent text message from {msg_data['sender']} to {msg_data['to']}")
            lora_out_queues[2].put((time.time(), header + payload_bytes), block = False)
            return True
        except Exception as e:
            print(f"[ERROR] Error serializing message: {e}")
            return False
        
    @staticmethod
    def publish_key(key_b64 : str, priority : int) -> bool:
        """
        Composes key publication message and puts it in the queue.
        """
        try:
            public_bytes = base64.b64decode(key_b64)
            
            if len(public_bytes) != 128:
                print("[ERROR] Public key must be exactly 128 bytes")
                return False

            ae_const = 0xAE
            kind = 0x01  # Key Publish
            current_utime = int(time.time())

            sender_key_id = struct.unpack("!I", public_bytes[:4])[0]

            key2 = public_bytes[4:]
            packet = struct.pack("!BBII124s", 
                                ae_const, 
                                kind, 
                                current_utime, 
                                sender_key_id, 
                                key2)
            with pending_keys_lock:
                if sender_key_id in pending_keys:
                    print(f"[DEBUG] Key for {sender_key_id} already in queue. Skipping.")
                    return False
        
            lora_out_queues[priority].put((time.time(), packet, sender_key_id), block = False) #TODO change later
            with pending_keys_lock:
                pending_keys.add(sender_key_id)
            print(f"[DEBUG] Key Publish packet for ID {sender_key_id} added to queue.")
            return True

        except Exception as e:
            print(f"[ERROR] Failed to publish key: {e}")
            return False
        
    @staticmethod
    def publish_user_details(key_id : int, encrypted_info : bytes):
        """
        User details creates a binary packet and sends it to the lora worker.
        user_data: dict which includes key_id, nick, name, phone
        """
        try:
            # Packing by the LoRa text protocol format
            header = struct.pack('!BBII', 
                                0xAE, 
                                0x02, 
                                int(time.time()), 
                                key_id)
            full_packet = header + encrypted_info
            lora_out_queues[4].put((time.time(), full_packet), block = False)
            print(f"User Details for {key_id} queued.")
        except Exception as e:
            print(f"[ERROR] Error publishing user details: {e}")

    @staticmethod
    def get_next_packet_wfq() -> tuple[int, tuple] | tuple[None, tuple[None, None]]:
        r = random.random()
        if r < WFQ_PRIORITY_WEIGHTS[0] and not lora_out_queues[1].empty(): 
            return 1, lora_out_queues[1].get()
        elif r < WFQ_PRIORITY_WEIGHTS[1] and not lora_out_queues[2].empty():
            return 2, lora_out_queues[2].get()
        elif r < WFQ_PRIORITY_WEIGHTS[2] and not lora_out_queues[3].empty():
            return 3, lora_out_queues[3].get()
        elif not lora_out_queues[4].empty():
            return 4, lora_out_queues[4].get()
        
        for p in [1, 2, 3, 4]:
            if not lora_out_queues[p].empty():
                return p, lora_out_queues[p].get()
        
        return None, (None, None)

    @staticmethod
    def lora_worker(device_path : str):
        """
        Worker who writes to the modem the first message in the queue
        """
        try:
            with LoRaModem(device_path) as modem:
                modem.configure_packet_mode()
                print(f"[DEBUG] LoRa Worker started on {device_path}")

                while True:
                    priority, item = LoRaWriter.get_next_packet_wfq()
                    if priority is None:
                        continue
                    print("[DEBUG] worker got a message")
                    if isinstance(item[1], bytes):
                        raw_bytes = item[1]
                        entry_time = item[0]
                        if time.time() > entry_time + QUEUE_ENTRY_TTL:
                            print("[DEBUG] Discarded message entered too long ago")
                            continue
                        modem.write_bytes(raw_bytes)
                        if priority == 1:
                            user_id = item[2] 
                            with pending_keys_lock:
                                pending_keys.discard(user_id)
                        with stats_lock:
                            server_stats["global_messages_sent"] += 1
                        print(f"[DEBUG] Worker sent {len(raw_bytes)} bytes. Priority: {priority}. Message kind {int(raw_bytes[1])}")
                    elif item[1] is not None:
                        print("[ERROR] non byte message in queue")
        except Exception as e:
            print(f"[ERROR] LoRa Worker crashed: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="LoRa Router implementing the REST API and LoRa text protocol")
    parser.add_argument("--port", type=int, default=8200, 
                        help="Port for the Flask REST API (default: 8200)")
    parser.add_argument("--device", type=str, default="sim", 
                        help="Modem device  (e.g., 1 for LoRa modem port, 'sim' for simulator) (default: 'sim')")
    parser.add_argument("--url", type=str, default='127.0.0.1',
                        help="url to run the REST API server on (default: 127.0.0.1)")
    args = parser.parse_args()
    threading.Thread(target=LoRaListener.lora_recieve_loop, args=(args.device,), daemon=True).start()
    threading.Thread(target=LoRaWriter.lora_worker, args=(args.device,), daemon=True).start()
    app.run(host=args.url, port=args.port)